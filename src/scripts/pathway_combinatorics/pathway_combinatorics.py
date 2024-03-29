import csv, itertools
from pygibbs.kegg_reaction import Reaction

def combine(fname, text_out):
	modules = {}
	for row in csv.DictReader(open(fname, 'r')):
		options = modules.setdefault(row['Module'], {})
		enzymes = options.setdefault(row['Option'], [])
		reaction = Reaction.FromFormula(row['Formula'])
		reaction.Balance(balance_water=False, exception_if_unknown=False)
		enzymes.append((row['Enzyme'], row['Formula'], float(row['Flux'])))
	
	l_mod = []
	for module, options in sorted(modules.iteritems()):
		l_opt = []
		for option, enzymes in sorted(options.iteritems()):
			l_opt.append(("%s%s" % (module, option), enzymes))
		l_mod.append(l_opt)
	
	for combination in itertools.product(*l_mod):
		entry = '_'.join([mod for mod, enzymes in combination])
		text_out.write('ENTRY       %s\n' % entry)
		text_out.write('THERMO      merged\n')
		firstrow = True
		for mod, enzymes in combination:
			for enzyme, formula, flux in enzymes:
				if firstrow:
					text_out.write('REACTION    %-6s %s (x%g)\n' %
								   (enzyme, formula, flux))
					firstrow = False
				else:
					text_out.write('            %-6s %s (x%g)\n' %
								   (enzyme, formula, flux))
		text_out.write('///\n')

def main():
	text_out = open('scripts/pathway_combinatorics/pathway_combinatorics.txt', 'w')
	combine('scripts/pathway_combinatorics/MOP_cycles.csv', text_out)
	combine('scripts/pathway_combinatorics/POR_cycles.csv', text_out)
	text_out.close()

if __name__ == "__main__":
	main()