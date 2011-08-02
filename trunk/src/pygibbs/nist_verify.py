#!/usr/bin/python

"""
    This script analyses the predictions of all the different estimation methods:
    Alberty, Hatzimanikatis, and the Milo lab Group Contribution method
"""

import logging
from toolbox.html_writer import HtmlWriter
from toolbox.database import SqliteDatabase
from pygibbs.hatzimanikatis import Hatzi
from pygibbs.nist import Nist
from pygibbs.thermodynamics import PsuedoisomerTableThermodynamics
from pygibbs.kegg import Kegg
from pygibbs.feist_ecoli import Feist

def LoadAllEstimators():
    db_public = SqliteDatabase('../data/public_data.sqlite')
    db_gibbs = SqliteDatabase('../res/gibbs.sqlite')
    db_tables = {'alberty': (db_public, 'alberty_pseudoisomers', 'Alberty'),
                 'PRC': (db_gibbs, 'nist_regression_pseudoisomers', 'our method (PRC)'),
                 'PGC': (db_gibbs, 'gc_pseudoisomers', 'our method (PGC)')}

    estimators = {}

    for key, (db, table_name, thermo_name) in db_tables.iteritems():
        if db.DoesTableExist(table_name):
            estimators[key] = PsuedoisomerTableThermodynamics.FromDatabase(
                                            db, table_name, name=thermo_name)
        else:
            logging.warning('The table %s does not exist in %s' % (table_name, str(db)))
    
    estimators['hatzi_gc'] = Hatzi(use_pKa=False)
    estimators['hatzi_gc_pka'] = Hatzi(use_pKa=True)
    return estimators

################################################################################################################
#                                                 MAIN                                                         #        
################################################################################################################

def main():
    estimators = LoadAllEstimators()
    html_writer = HtmlWriter("../res/nist/report.html")
    nist = Nist()
    nist.T_range = (273.15 + 24, 273.15 + 40)
    #nist.override_I = 0.25
    #nist.override_pMg = 14.0
    #nist.override_T = 298.15
    
    html_writer.write('<p>\n')
    html_writer.write("Total number of reaction in NIST: %d</br>\n" % len(nist.data))
    html_writer.write("Total number of reaction in range %.1fK < T < %.1fK: %d</br>\n" % \
                      (nist.T_range[0], nist.T_range[1], len(nist.SelectRowsFromNist())))
    html_writer.write('</p>\n')

    reactions = {}
    reactions['KEGG'] = Kegg.getInstance().AllReactions()
    reactions['FEIST'] = Feist.FromFiles().reactions
    reactions['NIST'] = nist.GetUniqueReactionSet()
    
    if False:
        nist.two_way_comparison(html_writer=html_writer, 
                                thermo1=estimators['alberty'],
                                thermo2=estimators['PRC'],
                                name='alberty_vs_nist')

        nist.two_way_comparison(html_writer=html_writer, 
                                thermo1=estimators['alberty'],
                                thermo2=estimators['PGC'],
                                name='alberty_vs_noor')

        nist.two_way_comparison(html_writer=html_writer, 
                                thermo1=estimators['alberty'],
                                thermo2=estimators['hatzi_gc'],
                                name='alberty_vs_jankowski')

        nist.two_way_comparison(html_writer=html_writer, 
                                thermo1=estimators['hatzi_gc'],
                                thermo2=estimators['PGC'],
                                name='jankowski_vs_noor')
        
        nist.two_way_comparison(html_writer=html_writer, 
                                thermo1=estimators['hatzi_gc'],
                                thermo2=estimators['hatzi_gc_pka'],
                                name='jankowski_pka')
        
    if True:
        estimators['alberty'].CompareOverKegg(html_writer, 
                                              other=estimators['PRC'],
                                              fig_name='kegg_compare_alberty_vs_nist')
    
    dict_list = []
    dict_list.append({'Method': 'Total', 'KEGG coverage':len(reactions['KEGG']),
                      'NIST coverage':len(reactions['NIST']),
                      'FEIST coverage':len(reactions['FEIST'])})
    for thermo_name, thermodynamics in estimators.iteritems():
        logging.info('Writing the NIST report for %s' % thermodynamics.name)
        html_writer.write('<p><b>%s</b> ' % thermodynamics.name)
        html_writer.insert_toggle(thermo_name)
        html_writer.start_div(thermo_name)
        num_estimations, rmse = nist.verify_results(html_writer=html_writer, 
                                                    thermodynamics=thermodynamics,
                                                    name=thermo_name)
        html_writer.end_div()
        html_writer.write('N = %d, RMSE = %.1f</p>\n' % (num_estimations, rmse))
        logging.info('N = %d, RMSE = %.1f' % (num_estimations, rmse))
        
        dict = {'Method':thermodynamics.name, 'RMSE (kJ/mol)':"%.1f (N=%d)" % (rmse, num_estimations)}
        for database_name, reaction_list in reactions.iteritems():
            n_covered = thermodynamics.CalculateCoverage(reaction_list)
            n_total = len(reaction_list)
            dict[database_name + " coverage"] = "%.1f%% (%d)" % \
                ((n_covered * 100.0 / n_total), n_covered)
        dict_list.append(dict)
    
    html_writer.write_table(dict_list, headers=['Method', 'RMSE (kJ/mol)', 
        'KEGG coverage', 'NIST coverage', 'FEIST coverage'])

if __name__ == '__main__':
    main()
