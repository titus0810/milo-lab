#!/usr/bin/env python

import json
import logging

from util import django_utils
from util import kegg

django_utils.SetupDjango()

from gibbs import models


def GetOrCreateNames(names_list):
    """Find all the names in the database.
    
    Create them if they are not present.
    """
    return [models.CommonName.GetOrCreate(n)
            for n in names_list] 
    
    
def GetReactions(rids_list):
    """Find all the given reactions in the database.
    
    Skip those that are not present.
    """
    rxns = []
    for rid in rids_list:
        try:
            rxns.append(models.StoredReaction.objects.get(kegg_id=rid))
        except Exception:
            logging.warning('Failed to retrieve reaction %s', rid)
            continue
    return rxns
        

def GetCompounds(cids_list):
    """Find all the given compounds in the database.
    
    Skip those that are not present.
    """
    compounds = []
    for kegg_id in cids_list:
        try:
            compounds.append(models.Compound.objects.get(kegg_id=kegg_id))
        except Exception:
            logging.warning('Failed to retrieve compound %s', rid)
            continue
    return compounds
        

COMPOUND_FILE = 'data/kegg_compounds.json'
REACTION_FILE = 'data/kegg_reactions.json'
ENZYME_FILE = 'data/kegg_enzymes.json'


def LoadKeggCompounds(kegg_json_filename=COMPOUND_FILE):
    parsed_json = json.load(open(kegg_json_filename))
    
    for cd in parsed_json:
        try:
            cid = cd['CID']
            
            formula = cd.get('formula')
            mass = cd.get('mass')
            if mass is not None:
                mass = float(mass)
            inchi = cd.get('InChI')
            num_electrons = cd.get('num_electrons')
            
            if formula is None:
                raise KeyError('Missing formula for CID %s' % cid)
            
            if mass is None:
                raise KeyError('Missing mass for CID %s' % cid)

            if inchi is None:
                raise KeyError('Missing inchi for CID %s' % cid)
                
            c = models.Compound(kegg_id=cid,
                                formula=formula,
                                inchi=inchi,
                                mass=mass)
            
            if num_electrons is not None:
                c.num_electrons = int(num_electrons)
                
            # Need to save before setting up many-to-many relationships.
            c.save()
                        
            names = GetOrCreateNames(cd['names'])            
            for n in names:
                c.common_names.add(n)
            c.save()        
        except Exception, e:
            logging.warning(e)
            continue
        

def LoadKeggReactions(reactions_json_filename=REACTION_FILE):
    parsed_json = json.load(open(reactions_json_filename))

    for rd in parsed_json:
        try:
            rid = rd['RID']
            rxn = rd['reaction']
            reactants = []
            products = []
            for coeff, cid in rxn:
                reactant = models.Reactant.GetOrCreate(cid, abs(coeff))
                if coeff < 0:
                    reactants.append(reactant)
                else:
                    products.append(reactant)
                
            # Need to save once.
            rxn = models.StoredReaction(kegg_id=rid)
            rxn.save()
            
            for reactant in reactants:
                rxn.reactants.add(reactant)
            for product in products:
                rxn.products.add(product)
            rxn.save()
        except Exception, e:
            logging.warning('Missing data for rid %s', rid)
            logging.error(e)
            continue


def LoadKeggEnzymes(enzymes_json_filename=ENZYME_FILE):
    parsed_json = json.load(open(enzymes_json_filename))

    for ed in parsed_json:
        try:
            ec = ed['EC']
            names = GetOrCreateNames(ed['names'])
            reactions = GetReactions(ed['reaction_ids'])
            substrates = GetCompounds(ed['substrates'])
            products = GetCompounds(ed['products'])
            cofactors = GetCompounds(ed['cofactors'])
            
            # Save first so we can do many-to-many mappings.
            enz = models.Enzyme(ec=ec)
            enz.save()
            
            # Add names, reactions, and compound mappings.
            map(enz.common_names.add, names)
            map(enz.reactions.add, reactions)
            map(enz.substrates.add, substrates)
            map(enz.products.add, products)
            map(enz.cofactors.add, cofactors)
            enz.save()
            
        except Exception, e:
            logging.warning('Missing data for ec %s', ec)
            logging.error(e)
            continue


def CheckData(filenames=(COMPOUND_FILE,
                         REACTION_FILE,
                         ENZYME_FILE)):
    for json_fname in filenames:
        json.load(open(json_fname))



def LoadAllKeggData():
    LoadKeggCompounds()
    LoadKeggReactions()
    LoadKeggEnzymes()    


if __name__ == '__main__':
    LoadAllKeggData()