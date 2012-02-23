#!/usr/bin/python

import csv
import logging
import numpy as np
import matplotlib.pyplot as plt
import os
import pylab
import sys

from optparse import OptionParser
from pygibbs.dissociation_constants import DissociationConstants,\
    MissingDissociationConstantError
from pygibbs.nist import Nist
from pygibbs.kegg import Kegg
from pygibbs.group_decomposition import GroupDecomposer
from pygibbs.thermodynamics import Thermodynamics,\
    MissingCompoundFormationEnergy, PsuedoisomerTableThermodynamics
from pygibbs.pseudoisomer import PseudoisomerMap
from pygibbs.thermodynamic_constants import default_I, default_pMg, default_T,\
    default_pH
from toolbox.linear_regression import LinearRegression
from toolbox.database import SqliteDatabase
from toolbox.html_writer import HtmlWriter, NullHtmlWriter
from toolbox.util import _mkdir
from toolbox.sparse_kernel import SparseKernel
from pygibbs.kegg_reaction import Reaction


class NistRegression(PsuedoisomerTableThermodynamics):
    
    def __init__(self, db, dissociation=None,
                 html_writer=None, nist=None):
        PsuedoisomerTableThermodynamics.__init__(self)
        self.db = db
        self.dissociation = dissociation or DissociationConstants.FromPublicDB()
        self.html_writer = html_writer or NullHtmlWriter()
        self.nist = nist or Nist()
        
        self.cid2pmap_dict = {}
        
        self.assume_no_pKa_by_default = False
        self.std_diff_threshold = np.inf
        
    def cid2PseudoisomerMap(self, cid):
        if cid in self.cid2pmap_dict:
            return self.cid2pmap_dict[cid]
        
        raise MissingCompoundFormationEnergy("The compound C%05d does not have a value for its formation energy of any of its pseudoisomers" % cid, cid)

    def get_all_cids(self):
        return sorted(self.cid2pmap_dict.keys())
    
    def ReverseTransformWithT(self):
        """Transform preserving temperature data."""
        logging.info("Reverse transforming the NIST data")
        nist_rows = self.nist.SelectRowsFromNist()
        nist_rows_normalized = [row.Clone() for row in nist_rows]               
        data = self.dissociation.ReverseTranformNistRows(nist_rows_normalized)
        stoichiometric_matrix = data['S']
        cids_to_estimate = data['cids_to_estimate']
        
        logging.info("%d out of %d NIST measurements can be used" %
                     (stoichiometric_matrix.shape[0], len(nist_rows_normalized)))
        
        # for every unique row, calculate the average dG0_r of all the rows that
        # are the same reaction
        dG0_r = data['dG0_r']
        temps = data['T']
        stoich_temps = stoichiometric_matrix * temps
        stoich_and_temps = pylab.hstack((stoichiometric_matrix, stoich_temps))
        
        print stoich_and_temps.shape
        
        return stoich_and_temps, dG0_r, cids_to_estimate
        
    
    def ReverseTransform(self, anchors=None, cid2nH_nMg=None):
        """
            Performs the reverse Legendre transform on all the data in NIST where
            it is possible, i.e. where all reactants have pKa values in the range
            (pH-2, pH+2) - the pH in which the Keq was measured.
            
            Arguments:
                anchors - an object of class Thermodynamics, which contains data
                          about the compounds which should have a fixed value of 
                          dG0 for one of their pseudoisomers.
        """
        logging.info("Reverse transforming the NIST data")
        nist_rows = self.nist.SelectRowsFromNist()
        logging.info("Selected %d NIST rows out of %d" %
                     (len(nist_rows), len(self.nist.data)))
        
        nist_rows_normalized = [row.Clone() for row in nist_rows]
        if anchors:
            # Use the known dG0 of the anchors as given, by subtracting their dG0 from
            # each reaction they participate in, and removing them from the regression
            # matrix.
            for row in nist_rows_normalized:
                row.NormalizeCompounds(anchors)

            self.override_data(anchors)
            self.anchors.update(anchors.get_all_cids())
        
        data = self.dissociation.ReverseTranformNistRows(nist_rows_normalized,
                                                         cid2nH_nMg=cid2nH_nMg)
                
        nist_rows_final = data['nist_rows']
        stoichiometric_matrix = data['S']
        cids_to_estimate = data['cids_to_estimate']
        n_cols = stoichiometric_matrix.shape[1]        
        
        if anchors:
            logging.info("%d out of %d compounds are anchored" % 
                         (len(anchors.get_all_cids()), len(cids_to_estimate)))
        logging.info("Only %d out of %d NIST measurements can be used" %
                     (n_cols, len(nist_rows_normalized)))

        # squeeze the regression matrix by leaving only unique rows
        unique_cols_S = np.unique([tuple(stoichiometric_matrix[:,i].flat) for i 
                                   in xrange(n_cols)])
        n_unique_cols = unique_cols_S.shape[1]

        logging.info("There are %d unique reactions" % n_unique_cols)
        unique_rids = set([nist_row.reaction.rid for nist_row in nist_rows
                            if nist_row.reaction.rid is not None])
        logging.info("Out of which %d have KEGG reaction IDs" % len(unique_rids))
        
        # for every unique column, calculate the average dG0_r of all the columns that
        # are the same reaction
        
        # full_data_mat will contain these columns: dG0, dG0_tag, dG0 - E[dG0], 
        # dG0_tag - E[dG0_tag], N
        # the averages are over the equivalence set of each reaction (i.e. the 
        # average dG of all the rows in NIST with that same reaction).
        # 'N' is the unique row number (i.e. the ID of the equivalence set)
        full_data_mat = np.matrix(np.zeros((5, n_cols)))
        for r in xrange(n_cols):
            full_data_mat[0, r] = data['dG0_r'][r]
            full_data_mat[1, r] = data['dG0_r_tag'][r]
        
        # unique_data_mat will contain these columns: E[dG0], E[dG0_tag],
        # std(dG0), std(dG0_tag), no. rows
        # there is exactly one row for each equivalence set (i.e. unique reaction)
        # no. rows holds the number of times this unique reaction appears in NIST
        unique_data_mat = np.matrix(np.zeros((5, n_unique_cols)))
        unique_sparse_reactions = []
        unique_nist_row_representatives = []
        for i in xrange(n_unique_cols):
            row_vector = unique_cols_S[:, i:i+1]
            
            # convert the rows of unique_rows_S to a list of sparse reactions
            sparse = {}
            for j in row_vector.nonzero()[1]: # 1 is the dimension of columns in S
                sparse[cids_to_estimate[j]] = unique_cols_S[j, i]
            reaction = Reaction(names=['NIST%03d' % i], sparse_reaction=sparse)
            unique_sparse_reactions.append(reaction)

            # find the list of indices which are equal to row i in unique_rows_S
            diff = abs(stoichiometric_matrix - np.repeat(row_vector, n_cols, 0))
            row_indices = np.where(np.sum(diff, 1) == 0)[0]
            unique_nist_row_representatives.append(nist_rows_final[row_indices[0]])
            
            # take the mean and std of the dG0_r of these rows
            sub_data_mat  = full_data_mat[row_indices, 0:2]
            unique_data_mat[i, 0:2] = np.mean(sub_data_mat, 0)
            unique_data_mat[i, 2:4] = np.std(sub_data_mat, 0)
            unique_data_mat[i, 4]   = sub_data_mat.shape[0]
            full_data_mat[row_indices, 4] = i
            full_data_mat[row_indices, 2:4] = sub_data_mat
            for k in row_indices:
                # subtract the mean from each row with this reaction
                full_data_mat[k, 2:4] -= unique_data_mat[i, 0:2]
                    
        # write a table that lists the variances of each unique reaction
        # before and after the reverse transform
        self.WriteUniqueReactionReport(unique_sparse_reactions,
                                       unique_nist_row_representatives,
                                       unique_data_mat, full_data_mat)
        
        return unique_rows_S, unique_data_mat[:, 0:1], cids_to_estimate

    def FindKernel(self, S, cids, sparse=True):
        sparse_kernel = SparseKernel(S)
        logging.info("Regression matrix is %d x %d, with a nullspace of rank %d" % \
                     (S.shape[0], S.shape[1], len(sparse_kernel)))
        
        # Remove non-zero columns
        if False:
            nonzero_columns = np.sum(abs(S), 0).nonzero()[0]
            S = S[:, nonzero_columns]
            cids = [cids[i] for i in nonzero_columns]

        logging.info("Finding the kernel of the stoichiometric matrix")

        dict_list = []
        if not sparse:
            K = LinearRegression.FindKernel(S)
            for i in xrange(K.shape[0]):
                reaction = Reaction.FromStoichiometricVector(K[i, :], cids)
                dict_list.append({'dimension':i,
                                  'kernel vector':reaction.to_hypertext()})
        else:
            try:
                for i, v in enumerate(sparse_kernel):
                    reaction = Reaction.FromStoichiometricVector(v, cids)
                    dict_list.append({'dimension':i,
                                      'kernel vector':reaction.to_hypertext()})
            except SparseKernel.LinearProgrammingException as e:
                print "Error when trying to find a sparse kernel: " + str(e)
        self.html_writer.write_table(dict_list, ['dimension', 'kernel vector'])
    
    def LinearRegression(self, S, dG0, cids, prior_thermodynamics=None):
        rankS = LinearRegression.MatrixRank(S)
        logging.info("Regression matrix is %d x %d, with a nullspace of rank %d" % \
                     (S.shape[0], S.shape[1], S.shape[1]-rankS))
        est_dG0_f, kerA = LinearRegression.LeastSquares(S, dG0)
        est_dG0_r = np.dot(S, est_dG0_f)
        residuals = est_dG0_r - dG0
        rmse = np.sqrt(np.mean(residuals**2))
        logging.info("Regression results for reverse transformed data:")
        logging.info("N = %d, RMSE = %.1f" % (S.shape[0], rmse))
        logging.info("Kernel rank = %d" % (kerA.shape[0]))

        if prior_thermodynamics:
            # find the vector in the solution subspace which is closest to the 
            # prior formation energies
            delta_dG0_f = np.zeros((0, 1))
            indices_in_prior = []
            for i, cid in enumerate(cids):
                try:
                    pmap = prior_thermodynamics.cid2PseudoisomerMap(cid)
                    for p_nH, unused_z, p_nMg, dG0 in sorted(pmap.ToMatrix()):
                        if p_nMg == 0:
                            dG0_base = self.ConvertPseudoisomer(cid, dG0, p_nH)
                            difference = dG0_base - est_dG0_f[i, 0]
                            delta_dG0_f = np.vstack([delta_dG0_f, difference])
                            indices_in_prior.append(i)
                except MissingCompoundFormationEnergy:
                    continue
                except MissingDissociationConstantError as e:
                    raise Exception("C%05d has no data about its dissociation "
                                    "constants: " + str(e))
            
            v, _ = LinearRegression.LeastSquares(kerA.T[indices_in_prior,:], 
                        delta_dG0_f, reduced_row_echlon=False)
            est_dG0_f += np.dot(kerA.T, v)

        # copy the solution into the diss_tables of all the compounds,
        # and then generate their PseudoisomerMaps.
        for i, cid in enumerate(cids):
            diss_table = self.dissociation.GetDissociationTable(cid)
            if diss_table is not None:
                diss_table.min_dG0 = est_dG0_f[i, 0]
                self.cid2pmap_dict[cid] = diss_table.GetPseudoisomerMap()
            else:
                self.cid2pmap_dict[cid] = PseudoisomerMap(nH=0, z=0, nMg=0,
                                            dG0=est_dG0_f[i, 0], ref='PRC')

    def WriteUniqueReactionReport(self, unique_sparse_reactions,
                                  unique_nist_row_representatives,
                                  unique_data_mat, full_data_mat,
                                  cid2nH_nMg=None):
        
        total_std = np.std(full_data_mat[:, 2:4], 0)
        
        fig = plt.figure()
        plt.plot(unique_data_mat[:, 2], unique_data_mat[:, 3], '.')
        plt.xlabel("$\sigma(\Delta_r G^\circ)$")
        plt.ylabel("$\sigma(\Delta_r G^{\'\circ})$")
        plt.title('$\sigma_{total}(\Delta_r G^\circ) = %.1f$ kJ/mol, '
                    '$\sigma_{total}(\Delta_r G^{\'\circ}) = %.1f$ kJ/mol' % 
                    (total_std[0], total_std[1]))
        self.html_writer.embed_matplotlib_figure(fig, width=640, height=480)
        logging.info('std(dG0_r) = %.1f' % total_std[0])
        logging.info('std(dG\'0_r) = %.1f' % total_std[1])
        
        rowdicts = []
        for i, reaction in enumerate(unique_sparse_reactions):
            logging.debug('Analyzing unique reaction: ' + 
                          str(unique_sparse_reactions[i]))
            data_row = unique_data_mat[i, :]
            ddG0 = self.dissociation.ReverseTransformReaction(reaction,
                pH=7, I=0.1, pMg=10, T=298.15, cid2nH_nMg=cid2nH_nMg)
            
            d = {}
            d["Reaction"] = reaction.to_hypertext(show_cids=False)
            d["reaction"] = reaction.FullReactionString(show_cids=False) # no hypertext for the CSV output
            d["Reference ID"] = unique_nist_row_representatives[i].ref_id
            d["EC"] = unique_nist_row_representatives[i].ec
            d["E(dG0)"] = "%.1f" % data_row[0]
            d["E(dG'0)"] = "%.1f" % data_row[1]
            d["E(dG0)'"] = "%.1f" % (data_row[0] + ddG0)
            d["std(dG0)"] = "%.1f" % data_row[2]
            d["std(dG'0)"] = "%.1f" % data_row[3]
            d["diff"] = data_row[2] - data_row[3]
            d["#observations"] = "%d" % data_row[4]
            
            flag = 0
            c_nad = reaction.sparse.get(3, 0)
            c_nadh = reaction.sparse.get(4, 0)
            c_nadp = reaction.sparse.get(6, 0)
            c_nadph = reaction.sparse.get(5, 0)
            if  c_nad == 1 and c_nadh == -1:
                flag = 1
            elif c_nad == -1 and c_nadh == 1:
                flag = -1
            elif c_nadp == 1 and c_nadph == -1:
                flag = 2
            elif c_nadp == -1 and c_nadph == 1:
                flag = -2
            d["Arren Flag"] = flag

            if d["diff"] > self.std_diff_threshold:
                _mkdir('../res/nist/reactions')
                link = "reactions/%s.html" % reaction.name
                d["analysis"] = '<a href="%s">link</a>' % link
                reaction_html_writer = HtmlWriter(os.path.join('../res/nist', link))
                self.AnalyzeSingleReaction(reaction,
                                           html_writer=reaction_html_writer)
            else:
                d["analysis"] = ''
            rowdicts.append(d)
        
        rowdicts.sort(key=lambda x:x["diff"], reverse=True)
        self.html_writer.write_table(rowdicts, ["Reaction", "Reference ID", "EC",
                         "#observations",
                         "E(dG0)", "E(dG'0)", "E(dG0)'",
                         "std(dG0)", "std(dG'0)",
                         "analysis"])
        csv_writer = csv.DictWriter(open('../res/nist_regression_unique.csv', 'w'),
                                    ["reaction", "Reference ID", "EC", "#observations",
                                     "E(dG0)", "E(dG'0)", "E(dG0)'",
                                     "std(dG0)", "std(dG'0)",'Arren Flag'],
                                    extrasaction='ignore')
        csv_writer.writeheader()
        csv_writer.writerows(rowdicts)

    def AnalyzeSingleReaction(self, reaction, html_writer=None):
        plt.rcParams['text.usetex'] = False
        plt.rcParams['font.family'] = 'sans-serif'
        plt.rcParams['font.size'] = 8
        plt.rcParams['lines.linewidth'] = 2
        plt.rcParams['lines.markersize'] = 8

        if not html_writer:
            html_writer = self.html_writer

        # gather all the measurements from NIST that correspond to this reaction
        logging.info("Analyzing reaction - " + str(reaction))
        nist_rows = self.nist.SelectRowsFromNist(reaction)
        logging.info("Found %d measurements in NIST with this reaction" %
                     len(nist_rows))
        html_writer.write('<h2>%s</h2>\n' % reaction.name)
        html_writer.write('<p>\nShow observation table: ')
        div_id = html_writer.insert_toggle()
        html_writer.div_start(div_id)
        dict_list = []
        for nist_row_data in nist_rows:
            d = {}
            d['pH'] = nist_row_data.pH
            d['I'] = nist_row_data.I
            d['pMg'] = nist_row_data.pMg
            d['dG\'0_r'] = "%.2f" % nist_row_data.dG0_r
            d['T(K)'] = nist_row_data.T
            if nist_row_data.url:
                d['URL'] = '<a href="%s">link</a>' % nist_row_data.url
            else:
                d['URL'] = ''
            dict_list.append(d)
        html_writer.write_table(dict_list, headers=['T(K)', 'pH', 'I', 'pMg', 'dG\'0_r', 'URL'])
        html_writer.div_end()
        html_writer.write('</p>\n')

        # reverse transform the data
        data = self.dissociation.ReverseTranformNistRows(nist_rows)
        
        html_writer.write('Reaction: %s</br>\n' % \
                          reaction.to_hypertext(show_cids=False))
        fig1 = plt.figure(figsize=[9.0, 6.0], dpi=80)
        html_writer.write('Standard deviations:</br>\n<ul>\n')
        
        y_labels = ['$\Delta_r G^{\'\circ}$', '$\Delta_r G^{\circ}$']
        x_limits = {'pH' : (3, 12), 'I' : (0, 1), 'pMg' : (0, 14)}
        
        for j, y_axis in enumerate(['dG0_r_tag', 'dG0_r']):
            sigma = np.std(data[y_axis])
            html_writer.write("  <li>stdev(%s) = %.2g</li>" % (y_axis, sigma))
            for i, x_axis in enumerate(['pH', 'I', 'pMg']):
                plt.subplot(2,3,i+3*j+1)
                plt.plot(data[x_axis], data[y_axis], '.')
                plt.xlim(x_limits[x_axis])
                if j == 1:
                    plt.xlabel(x_axis)
                if i == 0:
                    plt.ylabel(y_labels[j])
        html_writer.write('</ul>\n')
        html_writer.embed_matplotlib_figure(fig1)
        
        # draw the response of the graph to pH, I and pMg:
        fig2 = plt.figure(figsize=[9.0, 5.0], dpi=80)

        pH_range = np.arange(3, 12.01, 0.05)
        I_range = np.arange(0.0, 1.01, 0.01)
        pMg_range = np.arange(0.0, 14.01, 0.1)

        ddG_vs_pH = []
        for pH in pH_range:
            ddG = self.dissociation.ReverseTransformReaction(reaction, pH=pH, I=default_I, 
                                                pMg=default_pMg, T=default_T)
            ddG_vs_pH.append(ddG)
        
        ddG_vs_I = []
        for I in I_range:
            ddG = self.dissociation.ReverseTransformReaction(reaction, pH=default_pH, I=I, 
                                                pMg=default_pMg, T=default_T)
            ddG_vs_I.append(ddG)

        ddG_vs_pMg = []
        for pMg in pMg_range:
            ddG = self.dissociation.ReverseTransformReaction(reaction, pH=default_pH, I=default_I, 
                                                pMg=pMg, T=default_T)
            ddG_vs_pMg.append(ddG)
        
        plt.subplot(1, 3, 1)
        plt.plot(pH_range, ddG_vs_pH, 'g-')
        plt.xlabel('pH')
        plt.ylabel('$\Delta_r G^{\'\circ} - \Delta_r G^{\circ}$')
        plt.subplot(1, 3, 2)
        plt.plot(I_range, ddG_vs_I, 'b-')
        plt.xlabel('I')
        plt.subplot(1, 3, 3)
        plt.plot(pMg_range, ddG_vs_pMg, 'r-')
        plt.xlabel('pMg')
        html_writer.embed_matplotlib_figure(fig2, width=640, height=480)
        html_writer.write('</br>\n')
        
    def ConvertPseudoisomer(self, cid, dG0, nH_from, nH_to=None):
        return self.dissociation.ConvertPseudoisomer(cid, dG0, nH_from, nH_to)
    
    def Nist_pKas(self):
        kegg = Kegg.getInstance()
        group_decomposer = GroupDecomposer.FromDatabase(self.db)
        cids_in_nist = set(self.nist.cid2count.keys())
        cids_with_pKa = self.dissociation.GetAllCids()
        
        self.html_writer.write('CIDs with pKa: %d<br>\n' % len(cids_with_pKa))
        self.html_writer.write('CIDs in NIST: %d<br>\n' % len(cids_in_nist))
        self.html_writer.write('CIDs in NIST with pKas: %d<br>\n' % \
                          len(cids_in_nist.intersection(cids_with_pKa)))
        
        self.html_writer.write('All CIDs in NIST: <br>\n')
        self.html_writer.write('<table border="1">\n')
        self.html_writer.write('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>' % ("cid", "name", "count", "remark"))
        for cid, count in sorted(self.nist.cid2count.iteritems()):
            if cid not in cids_with_pKa:
                self.html_writer.write('<tr><td><a href="%s">C%05d<a></td><td>%s</td><td>%d</td><td>' % \
                    (kegg.cid2link(cid), cid, kegg.cid2name(cid), count))
                try:
                    mol = kegg.cid2mol(cid)
                    decomposition = group_decomposer.Decompose(mol, ignore_protonations=True, strict=True)
        
                    if len(decomposition.PseudoisomerVectors()) > 1:
                        self.html_writer.write('should have pKas')
                    else:
                        self.html_writer.write('doesn\'t have pKas')
                    self.html_writer.embed_molecule_as_png(
                        kegg.cid2mol(cid), 'png/C%05d.png' % cid)
                
                except Exception:
                    self.html_writer.write('cannot decompose')
                self.html_writer.write('</td></tr>\n')
        
        self.html_writer.write('</table>\n')

    def Calculate_pKa_and_pKMg(self, filename="../data/thermodynamics/dG0.csv"):
        kegg = Kegg.getInstance()
        cid2pmap = {}
        smiles_dict = {}
        
        for row in csv.DictReader(open(filename, 'r')):
            #smiles, cid, compound_name, dG0, unused_dH0, charge, hydrogens, Mg, use_for, ref, unused_assumption 
            name = "%s (z=%s, nH=%s, nMg=%s)" % (row['compound name'], row['z'], row['nH'], row['nMg'])
            logging.info('reading data for ' + name)
    
            if not row['dG0']:
                continue
    
            if (row['use for'] == "skip"):
                continue
                
            try:
                dG0 = float(row['dG0'])
            except ValueError:
                raise Exception("Invalid dG0: " + str(dG0))
    
            if (row['use for'] == "test"):
                pass
            elif (row['use for'] == "train"):
                pass
            else:
                raise Exception("Unknown usage flag: " + row['use for'])
    
            if row['cid']:
                cid = int(row['cid'])
                try:
                    nH = int(row['nH'])
                    z = int(row['z'])
                    nMg = int(row['nMg'])
                except ValueError:
                    raise Exception("can't read the data about %s" % (row['compound name']))
                cid2pmap.setdefault(cid, PseudoisomerMap())
                cid2pmap[cid].Add(nH, z, nMg, dG0)
    
            if row['smiles']:
                smiles_dict[cid, nH, z, nMg] = row['smiles']
            else: 
                smiles_dict[cid, nH, z, nMg] = ''
    
        #csv_writer = csv.writer(open('../res/pKa_from_dG0.csv', 'w'))
        
        self.self.html_writer.write('<table border="1">\n<tr><td>' + 
                          '</td><td>'.join(['cid', 'name', 'formula', 'nH', 'z', 'nMg', 'dG0_f', 'pKa', 'pK_Mg']) + 
                          '</td></tr>\n')
        for cid in sorted(cid2pmap.keys()):
            #step = 1
            for nH, z, nMg, dG0 in sorted(cid2pmap[cid].ToMatrix(), key=lambda x:(-x[2], -x[0])):
                pKa = cid2pmap[cid].GetpKa(nH, z, nMg)
                pK_Mg = cid2pmap[cid].GetpK_Mg(nH, z, nMg)
                self.self.html_writer.write('<tr><td>')
                self.self.html_writer.write('</td><td>'.join(["C%05d" % cid, 
                    kegg.cid2name(cid) or "?", 
                    kegg.cid2formula(cid) or "?", 
                    str(nH), str(z), str(nMg), 
                    "%.1f" % dG0, str(pKa), str(pK_Mg)]))
                #if not nMg and cid not in cid2pKa_list:
                #    csv_writer.writerow([cid, kegg.cid2name(cid), kegg.cid2formula(cid), step, None, "%.2f" % pKa, smiles_dict[cid, nH+1, z+1, nMg], smiles_dict[cid, nH, z, nMg]])
                #    step += 1
                self.self.html_writer.write('</td></tr>\n')
        self.self.html_writer.write('</table>\n')

    def WriteDataToHtml(self):
        Thermodynamics.WriteDataToHtml(self, self.html_writer)
        
    def VerifyResults(self):
        return self.nist.verify_results(html_writer=self.html_writer, 
                                        thermodynamics=self)

    def Train(self):
        # Train the formation energies using linear regression
        S, dG0, cids = self.ReverseTransform()
        self.LinearRegression(S, dG0, cids)
        self.ToDatabase(self.db, 'prc_pseudoisomers')
    
def MakeOpts():
    """Returns an OptionParser object with all the default options."""
    opt_parser = OptionParser()
    opt_parser.add_option("-k", "--kegg_database_location", 
                          dest="kegg_db_filename",
                          default="../data/public_data.sqlite",
                          help="The KEGG database location")
    opt_parser.add_option("-d", "--database_location", 
                          dest="db_filename",
                          default="../res/gibbs.sqlite",
                          help="The Thermodynamic database location")
    opt_parser.add_option("-t", "--thermodynamics_filename",
                          dest="thermodynamics_filename",
                          default='../data/thermodynamics/dG0.csv',
                          help="The name of the thermodynamics file to load.")
    opt_parser.add_option("-o", "--output_filename",
                          dest="output_filename",
                          default='../res/nist/regression.html',
                          help="Where to write output to.")
    return opt_parser


def main():
    options, _ = MakeOpts().parse_args(sys.argv)
    
    db_loc = options.db_filename
    logging.info('Reading from DB %s' % db_loc)
    
    public_db_loc = options.kegg_db_filename
    logging.info('Reading from public DB %s' % public_db_loc)
    
    output_filename = os.path.abspath(options.output_filename)
    logging.info('Will write output to %s' % output_filename)
    
    html_writer = HtmlWriter(output_filename)
    db = SqliteDatabase(db_loc)
    nist = Nist(T_range=None)
    nist_regression = NistRegression(db, html_writer=html_writer, nist=nist)
    nist_regression.std_diff_threshold = 20.0 # the threshold over which to print an analysis of a reaction
    #nist_regression.nist.T_range = None(273.15 + 24, 273.15 + 40)
    #nist_regression.nist.override_I = 0.25
    #nist_regression.nist.override_pMg = 14.0

    #nist_anchors = PsuedoisomerTableThermodynamics.FromCsvFile(
    #    '../data/thermodynamics/nist_anchors.csv')
    #
    #S, dG0, cids = nist_regression.ReverseTransform(nist_anchors)

    # copy the Alberty values from the public DB to the local DB
    #db_public = SqliteDatabase(public_db_loc)
    #alberty = PsuedoisomerTableThermodynamics.FromDatabase(db_public, 'alberty_pseudoisomers')

    html_writer.write("<h2>NIST regression:</h2>")
    nist_regression.Train()
    
    html_writer.write('<h3>PRC results:</h3>\n')
    html_writer.insert_toggle('regression')
    html_writer.div_start('regression')
    nist_regression.WriteDataToHtml()
    html_writer.div_end()

    html_writer.write('<h3>Reaction energies - PRC vs. Observed:</h3>\n')
    html_writer.insert_toggle('verify')
    html_writer.div_start('verify')
    N, rmse = nist_regression.VerifyResults()
    html_writer.div_end()
    html_writer.write('</br>\n')
    
    logging.info("Regression results for observed data:")
    logging.info("N = %d, RMSE = %.1f" % (N, rmse))

    html_writer.close()
    
if __name__ == "__main__":
    main()
