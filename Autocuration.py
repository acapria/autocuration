# -*- coding: utf-8 -*-

# This script is the full implementation of the Influenza autocuration pipeline for returning curation
# artifact flags of ingested influenza sequences.  See the Influenza autocuration SOP for a detailed
# description of the algorithms and methods for making up this framework.

import os
import re
import sys
import time
import pandas as pd
import numpy as np
import argparse
import subprocess
from Bio import SeqIO
from datetime import datetime
from itertools import groupby
from operator import itemgetter
from collections import Counter
from Bio.Blast.Applications import NcbiblastnCommandline
from Bio.Blast import NCBIXML


# This object should handle the curation of an input suquence, utilizing the profile alignments,
# lookup table, and boundary file
class Curation(object):

	# This object expects a SINGLE QUERY nucleotide sequence in fasta at minimum. Optionally, it can also take a 
	# strain name denoted as [Speicies]_[Segment #]_[Subtype].  The object can also take in a specified boundary 
	# file, lookup table file, and a directory name specifying the location of all the profile fasta files.  If
	# those required files are not inputted as arguments, the object navigates to the default location of these
	# files.
	def __init__(self, query, strain_name = None, boundary_file = "profiles/Flu_profile_boundaries_20181012.txt", 
		lookup_table = "profiles/Flu_profile_lookupTable_20181203.txt", profile_dir = "profiles", output_dir = "outputs"):
			
		# Grab accession number and nucleotide sequence string for query sequence in the fasta file
		accession, sequence = self.get_acc_and_seq(query)

		# Get the profile. Only BLAST if strain name not passed to init
		if strain_name:
			# Get the appropriate profile for the profile_dir based on the strain name
			try:
				profile = [filename for filename in os.listdir(profile_dir) if filename.startswith(strain_name)][0]
			except:
				raise Exception("\nERROR: Invalid profiles directory or strain name\n")
		else:
			# BLAST query to determine the appropriate profile and strain name
			b = Blast(query)
			profile = b.get_profile()
			identity = b.get_identity()
			strain_name = b.get_strain()

		# Check if BLAST result found profile. If not, abort the rest of the pipeline
		if profile == "Unknown":
			self.profile = "Unknown"
			self.identity = "Unknown"
			self.strain_name = "Unknown"
			self.accession = accession
			return

		# Parse boundary and lookup table text files
		boundary_df = self.parse_boundary_file(strain_name, boundary_file)
		lookup_df = self.parse_lookup_table(profile, lookup_table)

		# Determine the ambiguity flags, if any
		ambig_flags = []
		molseq = MolSeq(accession, sequence)
		if molseq.get_n_content() > 0.005:
			ambig_flags.append("Excess-N")
		if molseq.get_ambig_content() > 0.005:
			ambig_flags.append("Excess-Ambig")
		if identity < 0.80:
			ambig_flags.append("Excess-Dist")

		# Compute the alignment of the query to the profile using MUSCLE
		alignment = profile_dir+"/precomputed_alignment.fasta"
		cmd = './muscle -maxiters 1000 -profile -in1 '+profile_dir+'/'+profile+' -in2 '+query+' -out '+alignment
		subprocess.call(cmd, shell = True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

		# Initialize InDelSubs object to identify mutations from the alignment
		muts = InDelSubs(alignment)
		
		# Get the Flags
		del_flags =	muts.deletion_flags(boundary_df, lookup_df)
		ins_flags = muts.insertion_flags(boundary_df)
		sub_flags = muts.substitution_flags(boundary_df)

		mut_flags = del_flags + ins_flags + sub_flags

		# Only save alignment if no insertion flags
		if len(ins_flags) == 0:
			self.save_alignment(accession, alignment, output_dir)

		self.mut_flags = mut_flags
		self.del_flags = del_flags
		self.ins_flags = ins_flags
		self.sub_flags = sub_flags
		self.ambig_flags = ambig_flags
		self.profile = profile
		self.identity = identity
		self.strain_name = strain_name
		self.accession = accession

	# Return a table with all mutation flags occuring in the sequence, otherwise return 'Pass' if no flags
	# However, if no profile alignment was found in BLAST step, return 'Unknown'
	def mutation_flags(self):

		# Profile not known, just return unknown for mutation flags
		if self.profile == "Unknown":
			return("Unknown")

		if self.mut_flags == []:
			return("Pass")
		else:
			df = pd.DataFrame(self.mut_flags, columns = ['Flag', 'Profile Position', 'Query Position', 'Variant', 'Length'])
			return(df)

	# Return a ambiguity flag(s) for input query sequence
	# If no profile alignment was found in BLAST step, return 'Excess-Dist'
	def ambiguity_flags(self):

		# Since profile not known, sequence must be very different and therefore ambiguous
		if self.profile == "Unknown":
			return("Excess-Dist")

		if self.ambig_flags == []:
			return("Pass")
		else:
			return(self.ambig_flags)

	# Return the summary flag of the input query sequence
	# If ambiguity flags then 'Ambig-Seq' summary flag; if any CDS flags then 'Flag-CDS' summary flag;
	# if only CTS/NCR flags then 'Flag-NCR'; else 'Pass'
	def summary_flag(self):

		# If profile not known, sequence must be very different and therefore ambiguous
		if self.profile == "Unknown" or self.ambig_flags:
			return("Ambig-Seq")

		# Logical ordering of what to return for summary flags
		if True in [True for flag in self.mut_flags if "CDS" in flag[0]]:
			return("Flag-CDS")
		elif True in [True for flag in self.mut_flags if "NCR" in flag[0] or "CTS" in flag[0]]:
			return("Flag-NCR")	
		else:
			return("Pass")

	# Update Dr. Macken's Table 6 for curation bookeeping
	def update_table6(self, Table6 = 'outputs/Table6_Jan2019Release.txt'):

		# If no profile was found from BLAST (no strong hits), abort function
		# There is nothing to update for table 6
		if self.profile == "Unknown":
			return

		# If no flags, abort function
		# There is nothing to update for table 6
		if self.mut_flags == []:
			return

		try:
			table6 = pd.read_csv(Table6, sep = '\t')
		except:
			raise Exception("\nERROR: Invalid table6 directory and/or file\n")

		# Revert STATUS_THIS_MONTH to Unchanged for those that were updated last month
		# Revert CURRENT_MONTH_INCREASE count to zero if new month approached
		# Update PAST_MONTH_INCREASE count to the value of CURRENT_MONTH_INCREASE
		today = datetime.today()
		current_month = datetime.today().replace(day=1)
		table6['LAST_UPDATED'] = pd.to_datetime(table6['LAST_UPDATED'], errors='coerce').dt.floor('d')
		past_months = table6[(table6['LAST_UPDATED'] < current_month) & (table6['STATUS_THIS_MONTH'] != "Unchanged")]
		for ind in past_months.index:
			table6.at[ind, 'STATUS_THIS_MONTH'] = "Unchanged"
			table6.at[ind, 'PAST_MONTH_INCREASE'] = int(table6['CURRENT_MONTH_INCREASE'][ind])
			table6.at[ind, 'CURRENT_MONTH_INCREASE'] = 0

		# Loop through all detected flags
		for flag in self.mut_flags:
			if (flag[0] == "5'NCR-ext") or (flag[0] == "3'NCR-ext"):
				table6_profile = table6[(table6['PROFILE_NAME'] == self.profile) & (table6['FLU_SUBTYPE'] == self.strain_name) & 
				(table6['AUTO_ALIGNMENT_ISSUE'] == flag[0])]
				# Check if flag returns something already in the table
				if not table6_profile.empty:
					index = table6_profile.index[0]
					status = str(table6['STATUS_THIS_MONTH'][index])
					acc_list = set(str(table6['ACCESSION_LIST'][index]).split(","))
					if self.accession not in acc_list:
						acc_list.add(self.accession)
						acc_list = list(acc_list)
						acc_list.sort()
						acc_list = ",".join(acc_list)

						if status == "Unchanged":
							table6.at[index, 'STATUS_THIS_MONTH'] = str("Updated")
							table6.at[index, 'LAST_UPDATED'] = today
						table6.at[index, 'ACCESSION_TOTAL'] = int(table6['ACCESSION_TOTAL'][index])+1
						table6.at[index, 'CURRENT_MONTH_INCREASE'] = int(table6['CURRENT_MONTH_INCREASE'][index])+1
						table6.at[index, 'ACCESSION_LIST'] = str(acc_list)

				else:
					table6_profile = pd.DataFrame({'PROFILE_NAME': [self.profile], 'STATUS_THIS_MONTH': ["New"], 'LAST_UPDATED': [today], 
						'FLU_SUBTYPE': [self.strain_name], 'AUTO_ALIGNMENT_ISSUE': [flag[0]], 'POS_PROFILE': [""], 'MUTATION_SUM': [""], 
						'ACCESSION_TOTAL': [1], 'CURRENT_MONTH_INCREASE': [1], 'ACCESSION_LIST': [self.accession]})
					table6 = pd.concat([table6, table6_profile], axis = 0)

			else:
				table6_profile = table6[(table6['PROFILE_NAME'] == self.profile) & (table6['FLU_SUBTYPE'] == self.strain_name) & 
				(table6['AUTO_ALIGNMENT_ISSUE'] == flag[0]) & (table6['POS_PROFILE'] == flag[1])]
				if not table6_profile.empty:
					index = table6_profile.index[0]
					status = str(table6['STATUS_THIS_MONTH'][index])
					acc_list = set(str(table6['ACCESSION_LIST'][index]).split(","))
					if self.accession not in acc_list:
						acc_list.add(self.accession)
						acc_list = list(acc_list)
						acc_list.sort()
						acc_list = ",".join(acc_list)

						if status == "Unchanged":
							table6.at[index, 'STATUS_THIS_MONTH'] = str("Updated")
							table6.at[index, 'LAST_UPDATED'] = today
						table6.at[index, 'ACCESSION_TOTAL'] = int(table6['ACCESSION_TOTAL'][index])+1
						table6.at[index, 'CURRENT_MONTH_INCREASE'] = int(table6['CURRENT_MONTH_INCREASE'][index])+1
						table6.at[index, 'ACCESSION_LIST'] = str(acc_list)

						if flag[0] == "5'CTS-mut" or flag[0] == "3'CTS-mut":
							mut_sum = str(table6['MUTATION_SUM'][index])
							mut_sum = dict(item.split(":") for item in mut_sum.split(","))
							if mut_sum.get(flag[3]):
								mut_sum[flag[3]] = str(int(mut_sum[flag[3]])+1)
							else:
								mut_sum[flag[3]] = str(1)
							mut_sum = ",".join([key+':'+val for key, val in mut_sum.items()])
							table6.at[index, 'MUTATION_SUM'] = mut_sum
				
				else:
					mut_sum = ""
					if flag[0] == "5'CTS-mut" or flag[0] == "3'CTS-mut":
						mut_sum = flag[3]+':'+str(1)
					table6_profile = pd.DataFrame({'PROFILE_NAME': [self.profile], 'STATUS_THIS_MONTH': ["New"], 'LAST_UPDATED': [today], 
						'FLU_SUBTYPE': [self.strain_name], 'AUTO_ALIGNMENT_ISSUE': [flag[0]], 'POS_PROFILE': [flag[1]], 'MUTATION_SUM': [mut_sum], 
						'ACCESSION_TOTAL': [1], 'CURRENT_MONTH_INCREASE': [1], 'ACCESSION_LIST': [self.accession]})
					table6 = pd.concat([table6, table6_profile], axis = 0)

		table6 = table6.sort_values(by = ['PROFILE_NAME', 'ACCESSION_TOTAL'], ascending = [True, False]).reset_index(drop=True)
		table6.to_csv(Table6, sep = '\t', index = False)


	# Return just a table of the deletion flags, if any
	def deletion_flags(self):

		if self.profile == "Unknown":
			return("Unknown")

		if self.del_flags == []:
			return("Pass")
		else:
			df = pd.DataFrame(self.del_flags, columns = ['Flag', 'Profile Position', 'Query Position', 'Variant', 'Length'])
			return(df)

	# Return just a table of the insertion flags, if any
	def insertion_flags(self):

		if self.profile == "Unknown":
			return("Unknown")

		if self.ins_flags == []:
			return("Pass")
		else:
			df = pd.DataFrame(self.ins_flags, columns = ['Flag', 'Profile Position', 'Query Position', 'Variant', 'Length'])
			return(df)

	# Return just a table of the substitution flags, if any
	def substitution_flags(self):

		if self.profile == "Unknown":
			return("Unknown")

		if self.sub_flags == []:
			return("Pass")
		else:
			df = pd.DataFrame(self.sub_flags, columns = ['Flag', 'Profile Position', 'Query Position', 'Variant', 'Length'])
			return(df)

	# Return the file name of the profile
	def get_profile(self):

		return(self.profile)

	# Return the percent identity match to the top hit from the BLAST result
	def get_identity(self):

		if self.identity == "Unknown":
			return(self.identity)
		else:
			return(round(self.identity, 3))
	
	# Return the strain name of the virus denoted as [Speicies]_[Segment #]_[Subtype]
	def get_strain(self):

		return(self.strain_name)

	# Return the accession number of the query sequence
	def get_accession(self):

		return(self.accession)

	# Meant for grabbing accession number and nucleotide sequence string from the query sequence fasta file
	@staticmethod
	def get_acc_and_seq(query):
		
		for seq_record in SeqIO.parse(query, 'fasta'):
			metadata = str(seq_record.id)
			sequence = str(seq_record.seq).strip()
		
		acc = re.split(r'[^a-zA-Z0-9\s\w-]+', metadata)
		if re.search(r'[a-zA-Z]+', acc[0]) and re.search(r'[0-9]+', acc[0]):
			accession = acc[0]
		else:
			accession = acc[1]

		return(accession, sequence)

	# Meant for parsing the boundary file to know the CTS, NCR, and CDS start and end regions of the profile
	@staticmethod
	def parse_boundary_file(strain_name, boundary_file):

		try:
			boundary_types = open(boundary_file, 'r').readlines()
		except:
			raise Exception("\nERROR: Invalid boundary_file directory and/or file\n")
		boundary = ""
		for line in boundary_types:	
			if (line.startswith(strain_name)):
				boundary = line
				break

		boundary = boundary.split("|")
		boundary.pop(0)
		boundaries = {}
		for coord in boundary:
			feat = coord.split("=")[0]
			pos = int(coord.split("=")[1])
			boundaries[feat] = pos

		boundary_df = pd.DataFrame({"Region": ["CTS5", "NCR5", "CDS", "NCR3", "CTS3"],
			"Start": [boundaries['START'], boundaries['CTS5']+1, boundaries['ATG'], boundaries['STOP']+1, boundaries['CTS3']],
			"End": [boundaries['CTS5'], boundaries['ATG']-1, boundaries['STOP'], boundaries['CTS3']-1, boundaries['END']]})		

		return(boundary_df)

	# Meant to parse the lookup table so that we can use information for a specific strain.
	@staticmethod
	def parse_lookup_table(profile, lookup_table):

		try:
			lookup_open = open(lookup_table, 'r', encoding = "ISO-8859-1")
		except:
			raise Exception("\nERROR: Invalid lookup_table directory and/or file\n")
		lookup_allowed = lookup_open.readlines()
		profile_lookup = []
		for line in lookup_allowed:	
			if (line.startswith(profile)):
				line_attribs = line.split('\t')
				flag = line_attribs[1]
				start_end = line_attribs[2].split("..")
				if (len(start_end) == 2):
					start = int(start_end[0])
					end = int(start_end[1])
				else:
					start = int(start_end[0])
					end = int(start_end[0])
				lookup_df = pd.DataFrame({'Flag': [flag], 'Start': [start], 'End': [end]})
				profile_lookup.append(lookup_df)

		if profile_lookup != []:
			lookup_df = pd.concat(profile_lookup, ignore_index = True)
		else:
			lookup_df = pd.DataFrame({'Flag': ['XXX'], 'Start': [-1], 'End': [-1]})

		return(lookup_df)

	# Save the MUSCLE alignment if there were no insertions
	@staticmethod
	def save_alignment(query_acc, alignment, output_dir):

		with open(alignment, 'r') as file1:
			with open(output_dir+"/"+query_acc+"_aligned.fasta", 'w') as file2:
				for line in file1:
					file2.write(line)



# This is object is modified from the originally developed version by Christian Zmasek at JCVI.  It's used to handle a 
# query sequence, identify ambiguity in a sequence (indeterminate nucleotides or irregular characters), and write sequences
# back to a fasta
class MolSeq(object):

    # This object expects some the accession number of a sequence and the nucleotide sequence as a string of NAs.
    # Sequence in fasta in not supported for this object.
    def __init__(self, accession, sequence):
        
        self.seq_id = str(accession).strip()
        self.seq = str(sequence).strip()

    def get_seq_id(self):
        
        return self.seq_id

    def set_seq_id(self, id):
        
        self.seq_id = id

    def get_seq(self):
        
        return self.seq

    def get_length(self):
        
        return len(self.seq)

    def to_fasta(self):
        
        return ">{}\n{}".format(self.get_seq_id(), self.get_seq())

    # Counting the regular nucleotides of the nucelic acid sequence
    def count_regular_nucs(self):
        
        a = self.get_seq().count('a') + self.get_seq().count('A')
        c = self.get_seq().count('c') + self.get_seq().count('C')
        g = self.get_seq().count('g') + self.get_seq().count('G')
        t = self.get_seq().count('t') + self.get_seq().count('T')
        return a + c + g + t

    # Count the indeterminate, N's, of the NA sequence
    def count_indeterminate_nucs(self):
        
        return self.get_seq().count('n') + self.get_seq().count('N')

    # Return the percentage of Ns in the sequence
    def get_n_content(self):
        
        return (self.count_indeterminate_nucs() / self.get_length())

    # Return the percentage of ambiguity in the sequence
    def get_ambig_content(self):
        
        return ((self.get_length() - (self.count_regular_nucs() + self.count_indeterminate_nucs())) /
                self.get_length())



# This object is meant for detecting the positions of any insertions, deletions, or substitutions,
# in the query sequence upon alignment to the profile and then determines the artifact flag  due to
# those mutations.
class InDelSubs(object):

	# This object just expects the profile alignment in fasta, computed in a separate module.
	# This alignment is not assumed to be have preserved length of the original profile because
	# it needs to identify potential insertions.  Hence, the primary purpose of this init object
	# is to grab ahold of "non-keep-length" insertions and deletion positions for later computing
	# all mutations.
	def __init__(self, alignment):

		# Get all the sequences from the alignment
		sequences = []
		for seq_record in SeqIO.parse(alignment, 'fasta'):
			seq = str(seq_record.seq)
			sequences.append(seq)

		query_seq = sequences[len(sequences)-1]
		profile_seqs = sequences[:len(sequences)-1]

		# Deletion positions, denoted as gaps, within the query sequence.
		# These positions are with respect to the Non-Keeplength alginment
		# nkdp = Non-Keeplength Deletion Positions
		nkdp = set([i for i, ltr in enumerate(query_seq) if ltr == "-"])

		first_seq_dashes = set([i for i, ltr in enumerate(profile_seqs[0]) if ltr == "-"])
		profile_seqs_minus = profile_seqs[1:len(profile_seqs)]

		profile_dash_union = first_seq_dashes
		for seq in profile_seqs_minus:
			seq_dashes = set([i for i, ltr in enumerate(seq) if ltr == "-"])
			profile_dash_union = profile_dash_union.union(seq_dashes)

		# Required for computing the insertions positions
		profile_dash_intsct = first_seq_dashes
		for seq in profile_seqs_minus:
			seq_dashes = set([i for i, ltr in enumerate(seq) if ltr == "-"])
			profile_dash_intsct = profile_dash_intsct.intersection(seq_dashes)

		# Insertion positions, denoted as gaps conserved accross entire profile but
		# not in the query.  These positions are with respect to the Non-Keeplength alignment
		# nkip = Non-Keeplength Insertion Positions
		nkip = set(profile_dash_intsct - nkdp)

		# Accepted deletion positions are gaps/dashes that exist in the profile sequences.
		# These positions are with respect to the Non-Keeplength alignment.
		# nkadp = Non-Keeplenght Accepted Deletion Positions
		nkadp = set(profile_dash_union - nkip)

		# Group together non-keep-length sequential deletions as a list of lists
		del_groups = []
		flag_dels = list(set(set(nkdp) - set(nkadp)))
		flag_dels = np.array(flag_dels)
		flag_dels = np.sort(flag_dels)
		for k, g in groupby(enumerate(flag_dels), lambda ix: ix[0] - ix[1]):
			del_groups.append(list(map(itemgetter(1), g)))

		# Group together non-keep-length sequential insertions as a list of lists
		ins_groups = []
		nkip = np.array(list(nkip))
		nkip = np.sort(nkip)
		for k, g in groupby(enumerate(nkip), lambda ix: ix[0] - ix[1]):
			ins_groups.append(list(map(itemgetter(1), g)))

		self.nkdp = nkdp
		self.nkip = nkip
		self.nkadp = nkadp
		self.query_seq = query_seq
		self.profile_seqs = profile_seqs
		self.del_groups = del_groups
		self.ins_groups = ins_groups

	# Reports the flags triggered by deletions. Requires the processed boundary file and
	# lookup table file as pandas dataframes.
	def deletion_flags(self, boundary_df, lookup_df):

		# Needed for adjusting profile positions to query positions
		all_profile_ins = [(j-len([i for i in self.nkip if i <= j]))+1 for j in self.nkip]
		all_profile_del = [(j-len([i for i in self.nkip if i < j]))+1 for j in self.nkdp]

		flags = []
		for pos in self.del_groups:
			
			# Profile and query deletions in the group. profile_del has exact profile positions
			# but query_del has the positions in the query proceeding deletions event
			profile_del = [(j-len([i for i in self.nkip if i < j]))+1 for j in pos]
			query_del = [(j-len([i for i in self.nkdp if i <= j]))+1 for j in pos]

			# This will get ALL regions for which the deletion gaps may be overlapping
			regions = boundary_df[(boundary_df['Start'] <= profile_del[len(profile_del)-1]) & (boundary_df['End'] >= profile_del[0])]
			regions = regions.to_dict('records')
			
			# Loop through all possible regions for which gaps may be overlapping
			for reg in regions:
				region = reg['Region']
				
				# Annotated region start end with respect to profile
				start_p = reg['Start']
				end_p = reg['End']
				
				# Annotated region start end with respect to query
				# Take into account insertions and deletions occuring before the profile position
				start_q = start_p + len([i for i in all_profile_ins if i < start_p]) - len([j for j in all_profile_del if j <= start_p])
				end_q = end_p + len([i for i in all_profile_ins if i < end_p]) - len([j for j in all_profile_del if j <= end_p])

				# Positions of deletions in the annotated region with respect to profile and query
				region_dels_p = [i for i in profile_del if start_p <= i <= end_p]
				region_dels_q = [i for i in query_del if start_q <= i <= end_q]

				# This will not allow flagging of sequences that are simply shorter;
				# ie, gaps at the beginning or end of sequence relative to profile
				if (region_dels_q[0] == 0) or (region_dels_q[0] == (len(self.query_seq)-len(self.nkdp))):
					continue

				if len(pos) > 1:
					profile_pos = str(region_dels_p[0])+".."+str(region_dels_p[len(region_dels_p)-1])
				else:
					profile_pos = str(region_dels_p[0])
				query_pos = str(region_dels_q[0])+".."+str(region_dels_q[0]+1)

				# Go through each of the possible flags
				if (region == 'CTS5'):
					flags.append(tuple(("5'CTS-del", profile_pos, query_pos, "del", len(region_dels_p))))
				elif (region == 'CTS3'):
					flags.append(tuple(("3'CTS-del", profile_pos, query_pos, "del", len(region_dels_p))))
				elif (region == 'NCR5'):
					if self.check_lookup(lookup_df, "5'NCR-del", region_dels_p[0], region_dels_p[len(region_dels_p)-1]) == "FAIL":
						flags.append(tuple(("5'NCR-del", profile_pos, query_pos, "del", len(region_dels_p))))
				elif (region == 'NCR3'):
					if self.check_lookup(lookup_df, "3'NCR-del", region_dels_p[0], region_dels_p[len(region_dels_p)-1]) == "FAIL":
						flags.append(tuple(("3'NCR-del", profile_pos, query_pos, "del", len(region_dels_p))))
				elif (region == 'CDS'):
					if (len(pos) % 3) == 0:
						if self.check_lookup(lookup_df, "CDS-3Xdel", region_dels_p[0], region_dels_p[len(region_dels_p)-1]) == "FAIL":
							flags.append(tuple(("CDS-3Xdel", profile_pos, query_pos, "del", len(region_dels_p))))
					else:
						if self.check_lookup(lookup_df, "CDS-del", region_dels_p[0], region_dels_p[len(region_dels_p)-1]) == "FAIL":
							flags.append(tuple(("CDS-del", profile_pos, query_pos, "del", len(region_dels_p))))

		return flags

	# Reports the flags triggered by insertions. Requires the boundary file as pandas dataframe.
	def insertion_flags(self, boundary_df):

		profile_length = boundary_df['End'][4]

		flags = []
		for pos in self.ins_groups:

			ins_muts = self.query_seq[pos[0]:pos[len(pos)-1]+1].upper()

			profile_ins = [(j-len([i for i in self.nkip if i <= j]))+1 for j in pos]
			query_ins = [(j-len([i for i in self.nkdp if i < j]))+1 for j in pos]

			if len(pos) > 1:
				query_pos = str(query_ins[0])+".."+str(query_ins[len(query_ins)-1])
			else:
				query_pos = str(query_ins[0])
			profile_pos = str(profile_ins[0])+".."+str(profile_ins[0]+1)

			if (profile_ins[0] == 0):
				flags.append(tuple(("5'NCR-ext", profile_pos, query_pos, ins_muts, len(pos))))
				continue
			if (profile_ins[0] == profile_length):
				profile_pos = str(profile_ins[0])+".."
				flags.append(tuple(("3'NCR-ext", profile_pos, query_pos, ins_muts, len(pos))))
				continue

			region = boundary_df[(boundary_df['Start'] <= profile_ins[len(profile_ins)-1]) & (boundary_df['End'] >= profile_ins[0])]
			region = list(region['Region'])[0]

			# Go through each of the possible flags
			if (region == 'CTS5'):
				flags.append(tuple(("5'CTS-ins", profile_pos, query_pos, ins_muts, len(pos))))
			elif (region == 'CTS3'):
				flags.append(tuple(("3'CTS-ins", profile_pos, query_pos, ins_muts, len(pos))))
			elif (region == 'NCR5'):
				flags.append(tuple(("5'NCR-ins", profile_pos, query_pos, ins_muts, len(pos))))
			elif (region == 'NCR3'):
				flags.append(tuple(("3'NCR-ins", profile_pos, query_pos, ins_muts, len(pos))))
			elif (region == 'CDS'):
				if (len(pos) % 3) == 0:
					flags.append(tuple(("CDS-3Xins", profile_pos, query_pos, ins_muts, len(pos))))
				else:
					flags.append(tuple(("CDS-ins", profile_pos, query_pos, ins_muts, len(pos))))

		return flags

	# Reports the flags triggered by CTS substitutions. Requires the boundary file as pandas dataframe.
	def substitution_flags(self, boundary_df):

		CTS5 = boundary_df[(boundary_df['Region'] == 'CTS5')]
		CTS3 = boundary_df[(boundary_df['Region'] == 'CTS3')]

		# Get 5'/3' CTS start/end and adjust zero basing by subtracting 1
		CTS5_start = list(CTS5['Start'])[0] - 1
		CTS3_start = list(CTS3['Start'])[0] - 1
		CTS5_end = list(CTS5['End'])[0] - 1
		CTS3_end = list(CTS3['End'])[0] - 1

		# Adjust the 5'/3' CTS start/end to accommidate for the non-keep length alignment
		# by factoring in the positions of sequential deletions from the ins_groups
		CTS5_start_adj = CTS5_start
		CTS3_start_adj = CTS3_start + sum([len(self.ins_groups[i]) for i in range(len(self.ins_groups)) if self.ins_groups[i][0] < CTS3_start])
		CTS5_end_adj = CTS5_end + sum([len(self.ins_groups[i]) for i in range(len(self.ins_groups)) if self.ins_groups[i][0] < CTS5_end])
		CTS3_end_adj = CTS3_end + sum([len(self.ins_groups[i]) for i in range(len(self.ins_groups)) if self.ins_groups[i][0] < CTS3_end])

		# Get the CTS regions of the query sequence
		CTS5_query = self.query_seq[CTS5_start_adj:CTS5_end_adj+1]
		CTS3_query = self.query_seq[CTS3_start_adj:CTS3_end_adj+1]

		cts5_muts = [i for i in range(len(CTS5_query)) if CTS5_query[i] not in [seq[CTS5_start_adj:CTS5_end_adj+1][i]
			for seq in self.profile_seqs] and not ((i in self.nkip) or (i in self.nkdp))]

		cts3_muts = [i+CTS3_start_adj for i in range(len(CTS3_query)) if CTS3_query[i] not in [seq[CTS3_start_adj:CTS3_end_adj+1][i] 
			for seq in self.profile_seqs] and not ((i+CTS3_start_adj in self.nkip) or (i+CTS3_start_adj in self.nkdp))]

		cts5_positions = []
		for k, g in groupby(enumerate(cts5_muts), lambda ix: ix[0] - ix[1]):
			cts5_positions.append(list(map(itemgetter(1), g)))

		cts3_positions = []
		for k, g in groupby(enumerate(cts3_muts), lambda ix: ix[0] - ix[1]):
			cts3_positions.append(list(map(itemgetter(1), g)))

		flags = []

		#Check for 5'CTS mutations:
		for pos in cts5_positions:
			subs = self.query_seq[pos[0]:pos[len(pos)-1]+1].upper()
			# Check if the current substituions are just N characters, continue loop if so and do not flag
			if len(set(list(subs))) == 1 and list(set(list(subs)))[0] == 'N':
				continue
			profile_sub = [(j-len([i for i in self.nkip if i < j]))+1 for j in pos]
			query_sub = [(j-len([i for i in self.nkdp if i < j]))+1 for j in pos]

			if len(pos) > 1:
				profile_pos = str(profile_sub[0])+".."+str(profile_sub[len(profile_sub)-1])
				query_pos = str(query_sub[0])+".."+str(query_sub[len(query_sub)-1])
			else:
				profile_pos = str(profile_sub[0])
				query_pos = str(query_sub[0])

			flags.append(tuple(("5'CTS-mut", profile_pos, query_pos, subs, len(pos))))

		#Check for 3'CTS mutations:
		for pos in cts3_positions:
			subs = self.query_seq[pos[0]:pos[len(pos)-1]+1].upper()
			if len(set(list(subs))) == 1 and list(set(list(subs)))[0] == 'N':
				continue
			profile_sub = [(j-len([i for i in self.nkip if i < j]))+1 for j in pos]
			query_sub = [(j-len([i for i in self.nkdp if i < j]))+1 for j in pos]

			if len(pos) > 1:
				profile_pos = str(profile_sub[0])+".."+str(profile_sub[len(profile_sub)-1])
				query_pos = str(query_sub[0])+".."+str(query_sub[len(query_sub)-1])
			else:
				profile_pos = str(profile_sub[0])
				query_pos = str(query_sub[0])

			flags.append(tuple(("3'CTS-mut", profile_pos, query_pos, subs, len(pos))))

		return flags

	# Check the lookup table to determine if the flag gets accepted.
	@staticmethod
	def check_lookup(lookup_df, flag, start, end):

		table = lookup_df[(lookup_df['Flag'] == flag) & (lookup_df['Start'] <= start) & (lookup_df['End'] >= end)]

		if len(table) > 0:
			return("PASS")
		else:
			return("FAIL")



# This object is meant for BLASTing the query sequence against the profiles so that we can 
# ulimately deterimine the strain name ([Species]_[Segment]_[Subtype]) of the Influenza query
# sequence and use that to choose the profile and guide the rest of auto-curation.
class Blast(object):

	# Intialize the blasting object by passing in the query sequence and optional blast database.
	# The blast database should be pre-computed and stored somewhere, otherwise, a blast database
	# can be passed into the init function.  The default parameters are the default locations of the
	# blast database and blast command line program.
	def __init__(self, query, blast_db = 'blast/flu_profiles_db.fasta', blastn_cmd = 'blast/blastn', 
		blast_result = 'blast/blast_result.xml'):

		# Blast query against database of profile sequences
		cmdline = NcbiblastnCommandline(cmd=blastn_cmd, query=query, db=blast_db, outfmt=5, out=blast_result)
		stdout, stderr = cmdline()

		result_handle = open(blast_result)
		for result in NCBIXML.parse(result_handle):
			if len(result.alignments) > 0:
				profile = result.alignments[0].title.split("|")[3]
				identity = float(result.alignments[0].hsps[0].identities)/float(result.alignments[0].hsps[0].align_length)
			else:
				profile = "Unknown"
				identity = "Unknown"
			break

		self.profile = profile
		self.identity = identity
	
	# Return the profile name mapping to the query sequence
	def get_profile(self):
		
		return(self.profile)

	# Return the percent identity of the query blast result
	def get_identity(self):

		return(self.identity)

	# Return the strain name of the query
	def get_strain(self):

		if self.profile == "Unknown":
			strain = "Unknown"
		else:
			strain = self.profile.split("_")
			strain = strain[:len(strain)-1]
			strain = "_".join(strain)

		return(strain)


# Main program for running the whole script from commandline
# Required argument: --query [QUERY FASTA]
# Optional argument: --flag [muts/ambig/ins/del/sub] (ie, the type of flags to return)
# Optional argument: --table6 [TABLE6 FILE PATH]
if __name__ == "__main__":

	parser = argparse.ArgumentParser()

	# Required argument
	parser.add_argument('--query', dest = 'query', type = str)
	
	# Optional arguments
	parser.add_argument('--flag', dest = 'flag', type = str)
	parser.add_argument('--table6', dest = 'table6', type = str)
	args = parser.parse_args()

	if (not args.query):
		sys.exit("\nERROR: No query sequence input\n")
	if (args.flag and (args.flag != 'mut' and args.flag != 'ambig' and args.flag != 'ins' and args.flag != 'del' and args.flag != 'sub')):
		sys.exit("\nERROR: Invalid flag argument\n --flag [all/ambig/ins/del/sub]\n")
	if (args.table6):
		table6 = args.table6
	else:
		table6 = 'outputs/Table6_Jan2019Release.txt'

	for seq_record in SeqIO.parse(args.query, 'fasta'):
		seq_id = str(seq_record.id)
		seq = str(seq_record.seq)
		seq_fasta = MolSeq(seq_id, seq).to_fasta()
		with open('query.fasta', 'w') as f:	f.write(seq_fasta)
		start = time.time()
		cur = Curation('query.fasta')
		if not args.flag:
			print("Accession:", cur.get_accession())
			print("Subtype:", cur.get_strain())
			print("Percent Identity:", cur.get_identity())
			print("Summary Flag:", cur.summary_flag())
			print("Ambiguity Flags:", cur.ambiguity_flags())
			print("Mutation Flags:\n", cur.mutation_flags())
			print('\n')
		elif args.flag == 'mut':
			print("Accession:", cur.get_accession())
			print("Subtype:", cur.get_strain())
			print("Percent Identity:", cur.get_identity())
			print("Mutation Flags:\n", cur.mutation_flags())
			print('\n')
		elif args.flag == 'ambig':
			print("Accession:", cur.get_accession())
			print("Subtype:", cur.get_strain())
			print("Percent Identity:", cur.get_identity())
			print("Summary Flag:", cur.summary_flag())
			print("Ambiguity Flags:", cur.ambiguity_flags())
			print('\n')
		elif args.flag == 'ins':
			print("Accession:", cur.get_accession())
			print("Subtype:", cur.get_strain())
			print("Percent Identity:", cur.get_identity())
			print("Summary Flag:", cur.summary_flag())
			print("Insertion Flags:\n", cur.insertion_flags())
			print('\n')
		elif args.flag == 'del':
			print("Accession:", cur.get_accession())
			print("Subtype:", cur.get_strain())
			print("Percent Identity:", cur.get_identity())
			print("Summary Flag:", cur.summary_flag())
			print("Deletion Flags:\n", cur.deletion_flags())
			print('\n')
		elif args.flag == 'sub':
			print("Accession:", cur.get_accession())
			print("Subtype:", cur.get_strain())
			print("Percent Identity:", cur.get_identity())
			print("Summary Flag:", cur.summary_flag())
			print("Substitution Flags:\n", cur.substitution_flags())
			print('\n')
		cur.update_table6(Table6 = table6)
		end = time.time()
		print("Compute time for sequence:", round(end - start, 3))
		print('\n')






