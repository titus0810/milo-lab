import numpy as np
import matplotlib.pyplot as plt
import logging
from pygibbs.kegg_parser import ParsedKeggFile
from pygibbs.pathway import PathwayData
from toolbox.html_writer import HtmlWriter
from pygibbs.pathway_modelling import KeggPathway, DeltaGNormalization
from pygibbs.thermodynamic_estimators import LoadAllEstimators
from pygibbs.metabolic_modelling.mtdf_optimizer import UnsolvableConvexProblemException
from pygibbs.thermodynamic_constants import R, default_I, default_pH,\
    default_pMg, default_T
from pygibbs.kegg_reaction import Reaction
import cvxpy

RT = R * default_T


def get_concentration_bounds(cids, cid2bounds, c_range):
    ln_lbs = np.zeros((len(cids), 1))
    ln_ubs = np.zeros((len(cids), 1))
    for i, cid in enumerate(cids):
        lb, ub = cid2bounds.get(cid, c_range)
        ln_lbs[i, 0] = np.log(lb or c_range[0])
        ln_ubs[i, 0] = np.log(ub or c_range[1])

    return ln_lbs, ln_ubs
    
def pareto(html_writer, estimators, pH=default_pH, I=default_I, T=default_T, pMg=default_pMg,
           c_range=(1e-6, 1e-2), cid2bounds=None,
           filename='../data/thermodynamics/odfe_vs_atp_carbon_fixation.txt',
           plot_profile=False, section_prefix=""):
    
    entry2fields_map = ParsedKeggFile.FromKeggFile(filename)
    cid2bounds = cid2bounds or {}
    entries = entry2fields_map.entries()
    plot_data = np.zeros((len(entries), 4))
    
    html_writer.write('<h2 id="%s_tables">Individual result tables</h1>\n' % section_prefix)
    remarks = []
    good_entries = []
    for i, entry in enumerate(entries):
        field_map = entry2fields_map[entry]
        p_data = PathwayData.FromFieldMap(field_map)
        
        if p_data.skip:
            logging.info("Skipping pathway: %s", entry)
            remarks.append('skipping')
            continue

        #html_writer.write('<a name="%s"></a>\n' % entry)
        html_writer.write('<h3 id="%s_%s">%s</h2>\n' % (section_prefix, entry, entry))
        thermo = estimators[field_map.get('THERMO', 'merged')]
        thermo.SetConditions(pH=pH, I=I, T=T, pMg=pMg)

        S, rids, fluxes, cids = p_data.get_explicit_reactions()
        dG0_r_prime = thermo.GetTransfromedReactionEnergies(S, cids)
        if np.any(np.isnan(dG0_r_prime)):
            remarks.append('NaN reaction energy')
            continue

        motive_force_lb = cvxpy.variable(name='ODB')
        ln_conc = cvxpy.variable(len(cids), 1, name='lnC')
        dG_r_prime = cvxpy.matrix(dG0_r_prime) + RT * cvxpy.matrix(S) * ln_conc
        
        ln_lbs, ln_ubs = get_concentration_bounds(cids, cid2bounds, c_range)
        constraints = cvxpy.geq(ln_conc, cvxpy.matrix(ln_lbs)) +\
                      cvxpy.leq(ln_conc, cvxpy.matrix(ln_ubs))
        
        ##### Maximize the lower-bound on the driving force (ODB)
        
        # Add the constraints on the reaction motive forces
        for j in xrange(len(rids)):
            curr_motive_force = -dG_r_prime[j, 0] * fluxes[j]
            constraints.append(cvxpy.geq(curr_motive_force, motive_force_lb))
        
        program_mtdf = cvxpy.program(cvxpy.maximize(motive_force_lb), constraints)
        program_mtdf.solve(quiet=True)
        odb = motive_force_lb.value
        logging.info('%20s: ODB = %.1f [kJ/mol]' % (entry, odb))
        odfe = 100 * np.tanh(odb / (2*R*thermo.T))
        
        ##### Maximize the total dG' used by the pathway
        
        # add the ODB value as a constraint
        constraints.append(cvxpy.eq(motive_force_lb, odb)) 
        total_dG_prime = cvxpy.matrix(fluxes) * dG_r_prime
        
        program_total = cvxpy.program(cvxpy.maximize(total_dG_prime), constraints)
        program_total.solve(quiet=True)
        optimal_total_dG = total_dG_prime.value
        concentrations = np.exp(ln_conc.value)
        
        good_entries.append(i)
        remarks.append('okay')
        plot_data[i, :] = [odb, odfe, optimal_total_dG, np.sum(fluxes)]

        html_writer.write_ul(["ODB = %.1f [kJ/mol]" % odb,
                              "ODFE = %.1f%%" % odfe,
                              "Total &#x394;<sub>r</sub>G' = %.1f [kJ/mol]" % optimal_total_dG])
        keggpath = KeggPathway(S, rids, fluxes, cids, None, dG0_r_prime,
                               cid2bounds=cid2bounds, c_range=c_range)
        keggpath.WriteProfileToHtmlTable(html_writer, concentrations)
        keggpath.WriteConcentrationsToHtmlTable(html_writer, concentrations)

    html_writer.write('<h2 id="%s_summary">Summary table</h1>\n' % section_prefix)
    dict_list = [{'Name':'<a href="#%s_%s">%s</a>' % (section_prefix, entries[i], entries[i]),
                  'ODB [kJ/mol]':'%.1f' % plot_data[i, 0],
                  'ODFE':'%.1f%%' % plot_data[i, 1],
                  'Total dG\' [kJ/mol]':'%.1f' % plot_data[i, 2],
                  'sum(flux)':'%g' % plot_data[i, 3],
                  'remark':remarks[i]}
                 for i in xrange(len(entries))]
    html_writer.write_table(dict_list,
        headers=['Name', 'ODB [kJ/mol]', 'ODFE', 'Total dG\' [kJ/mol]', 'sum(flux)', 'remark'])
            
    fig = plt.figure(figsize=(6, 6), dpi=90)
    plt.plot(plot_data[good_entries, 2], plot_data[good_entries, 0], '.', figure=fig)
    for i in good_entries:
        if plot_data[i, 0] < 0:
            color = 'grey'
        else:
            color = 'black'
        plt.text(plot_data[i, 2], plot_data[i, 0], entries[i],
                 ha='center', va='bottom', fontsize=6, color=color)
    plt.xlabel('Optimal Energetic Efficiency [kJ/mol]', figure=fig)
    plt.ylabel('Optimized Distributed Bottleneck [kJ/mol]', figure=fig)
    fig.axes[0].set_xlim(min(plot_data[good_entries, 2])*1.1, 0)
    fig.axes[0].set_ylim(0, max(plot_data[good_entries, 0])*1.1)
    return fig
    
if __name__ == "__main__":
    estimators = LoadAllEstimators()
    output_filename = '../res/odfe_vs_atp_carbon_fixation.html'
    html_writer = HtmlWriter(output_filename)
    cid2bounds = {
                  1:    (1,    1),    # water
                  #11:   (1e-5, 1e-5), # CO2
                  #288:  (9e-5, 9e-5), # carbonate
                  2:    (5e-3, 5e-3), # ATP (in order to keep ATP -> ADP at -55 kJ/mol)
                  8:    (5e-4, 5e-4), # ADP (in order to keep ATP -> ADP at -55 kJ/mol)
                  9:    (5e-3, 5e-3), # Pi  (in order to keep ATP -> ADP at -55 kJ/mol)
                  20:   (1e-4, 1e-4), # AMP (in order to keep ATP -> AMP at -110 kJ/mol)
                  13:   (3e-9, 3e-9), # PPi (in order to keep ATP -> AMP at -110 kJ/mol)
                  #20:   (5e-3, 5e-3), # AMP (in order to keep ATP -> AMP at -55 kJ/mol)
                  #13:   (5e-2, 5e-2), # PPi (in order to keep ATP -> AMP at -55 kJ/mol)
                  3:    (1e-4, 1e-3), # NAD_ox
                  4:    (1e-4, 1e-3), # NAD_red (to keep ox/red between 0.1 and 10)
                  6:    (1e-4, 1e-3), # NADP_ox
                  5:    (1e-4, 1e-3), # NADP_red (to keep ox/red between 0.1 and 10)
                  399:  (1e-4, 1e-3), # ubiquinone_ox
                  390:  (1e-4, 1e-3), # ubiquinol_red (to keep ox/red between 0.1 and 10)
                  139:  (1e-4, 1e-3), # ferredoxin_ox
                  138:  (1e-4, 1e-3), # ferredoxin_red (to keep ox/red between 0.1 and 10)
                  
                  828:  (1e-4, 1e-3), # menaquinone_ox
                  5819: (1e-4, 1e-3), # menaquinone_red
                  343:  (1e-4, 1e-3), # thioredoxin_ox
                  342:  (1e-4, 1e-3), # thioredoxin_red

                  #3:   (5e-3, 5e-3), # NAD_ox
                  #4:   (5e-5, 5e-5), # NAD_red (to keep ox/red at 100)
                  #6:   (5e-5, 5e-5), # NADP_ox
                  #5:   (5e-4, 5e-4), # NADP_red (to keep ox/red at 0.1)
                  #399: (1e-3, 1e-3), # ubiquinone_ox
                  #390: (1e-4, 1e-2), # ubiquinol_red (to keep ox/red between 0.1 and 10)
                  #139: (1e-6, 1e-6), # ferredoxin_ox
                  #138: (1e-2, 1e-2), # ferredoxin_red (to keep ox/red at 1e-4)
                  }
    co2_hydration = Reaction.FromFormula("C00011 + C00001 => C00288")
    
    I, T, pMg = 0.1, 298.15, 10
    for pH in [7]:
        co2_hydration_dG0_prime = estimators['alberty'].GetTransfromedKeggReactionEnergies([co2_hydration])[0, 0]
        for co2_conc in [1e-5]:
            carbonate_conc = co2_conc * np.exp(-co2_hydration_dG0_prime / (R*T))
            cid2bounds[11] = (co2_conc, co2_conc)
            cid2bounds[288] = (carbonate_conc, carbonate_conc)
            
            section_prefix = 'pH_%g_CO2_%g' % (pH, co2_conc*1000)
            section_title = 'pH = %g, [CO2] = %g mM' % (pH, co2_conc*1000)
            html_writer.write('<h1 id="%s_title">%s</h1>\n' %
                              (section_prefix, section_title))
            html_writer.write_ul(['<a href="#%s_tables">Individual result tables</a>' % section_prefix,
                                  '<a href="#%s_summary">Summary table</a>' % section_prefix,
                                  '<a href="#%s_figure">Summary figure</a>' % section_prefix])

            fig = pareto(html_writer, estimators,
                         pH=pH, I=I, T=T, pMg=pMg, cid2bounds=cid2bounds,
                         section_prefix=section_prefix)
            html_writer.write('<h2 id="%s_figure">Summary figure</h1>\n' % section_prefix)
            plt.title(section_title, figure=fig)
            html_writer.embed_matplotlib_figure(fig)
    
    html_writer.close()
