import pandas as pd
import numpy as np

from fuzzywuzzy import fuzz
from tqdm import tqdm

# @TODO(RAY): switch this from notebook format to functional

MATCH_PATH = "/ssd-1/clinical/clinical-abbreviations/data/full_prediction.csv"
RECORD_PATH = "/ssd-1/clinical/clinical-abbreviations/code/Step3Output.csv"
MATCH_THRESHOLD = .78
eliminate_list = ['ribo', 'non', 'gene', 'acid'] # Terms that result in suspicious matches; to be manually unmatched

def _remove_suspicious_matches(df_row):
    for item in eliminate_list:
        in_row1 = item in df_row["LF1"]
        in_row2 = item in df_row["LF2"]
        if in_row1 != in_row2:
            return 0
    return df_row['match_score']


# Read data
full_df = pd.read_csv(MATCH_PATH, na_filter=False)
record_df = pd.read_csv(RECORD_PATH, sep='|', na_filter=False)

record_df.replace(r'^\s*$', np.nan, regex=True, inplace=True)
full_df.replace(r'^\s*$', np.nan, regex=True, inplace=True)
full_df.columns = ['LF1', 'LF2', 'RecordID1', 'RecordID2', 'match_score']
full_df.sort_values(by=['LF1'], axis=0, ascending=True)
full_df = full_df.reset_index(inplace=False, drop=True)


# Match entries by LFEUI in 'group' column

lfeui_match_df = record_df.dropna(axis=0, how='any', subset= ['LFEUI'], inplace=False)
current_groups = lfeui_match_df[['SF', 'LFEUI']].groupby(['SF', 'LFEUI'], axis=0)['SF'].size().reset_index(name='Size')
current_groups = current_groups[current_groups["Size"] > 1]
cur_group_id = current_groups.shape[0]
current_groups['group'] = range(cur_group_id)
merged_record_df = record_df.merge(current_groups[['SF', 'LFEUI', 'group']], how='left', on=['SF', 'LFEUI'])
merged_record_df['group'].fillna(0, inplace=True)

# Match identical long forms in 'group2' column
lf_match_df = merged_record_df[merged_record_df['LFEUI'].isnull()]
current_groups = lf_match_df[['SF', 'LF', 'group']].groupby(['SF', 'LF'], axis=0)['LF'].size().reset_index(name='Size')
current_groups = current_groups[current_groups["Size"] > 1]
current_groups['group2'] = range(cur_group_id, cur_group_id + len(current_groups))
cur_group_id = cur_group_id + len(current_groups)

grouped_lf_match_df = lf_match_df.merge(current_groups[['SF', 'LF', 'group2']], how='left', on=['SF', 'LF'])
merged_record_df_2 = merged_record_df.merge(grouped_lf_match_df[['RecordID', 'group2']], how='left', on=['RecordID'])
merged_record_df_2['group2'].fillna(0, inplace=True)

# Merge LFEUI and long form matches together (these sets are nonintersecting, so addition is fine)
merged_record_df_2['group'] = merged_record_df_2['group'] + merged_record_df_2['group2']

# Only keep matches above the decided threshold
match_df = full_df[full_df['match_score'] > MATCH_THRESHOLD]
match_df = match_df.reset_index(inplace=False, drop=True)
match_df['match_score'] = match_df.apply(lambda x: _remove_suspicious_matches(x), axis=1)

group_ids = merged_record_df_2[['RecordID', 'group']]
group_ids.set_index('RecordID', inplace=True)


group_equivalencies = []
for inx, row in full_df.iterrows():

    if row['match_score'] > MATCH_THRESHOLD:
        id_1 = row["RecordID1"]
        id_2 = row["RecordID2"]
        # If neither has an assigned group, assign both to a group
        if group_ids.loc[id_1, 'group'] == 0 and group_ids.loc[id_2, 'group'] == 0:
            cur_group = cur_group_id
            group_ids.loc[id_1, 'group'] = cur_group
            group_ids.loc[id_2, 'group'] = cur_group
            cur_group_id += 1

        # If one has an assigned group, assign the other to that group
        elif group_ids.loc[id_1, 'group'] == 0 and group_ids.loc[id_2, 'group'] != 0:
            cur_group = group_ids.loc[id_2, 'group']
            group_ids.loc[id_1, 'group'] = cur_group

        elif group_ids.loc[id_1, 'group'] != 0 and group_ids.loc[id_2, 'group'] == 0:
            cur_group = group_ids.loc[id_1, 'group']
            group_ids.loc[id_2, 'group'] = cur_group

        # Else add the two groups to a set of group equivalencies to fix after the fact
        else:
            if group_ids.loc[id_1, 'group'] != group_ids.loc[id_2, 'group']:
                group_equivalencies.append([group_ids.loc[id_1, 'group'], group_ids.loc[id_2, 'group']])


# Fix group equivalencies, per the above
group_equivalencies_set = [(min(sample), max(sample)) for sample in group_equivalencies]
group_equivalencies_set = set(group_equivalencies_set)
group_equivalencies_set = sorted(group_equivalencies_set, key=lambda x: x[1])
equivalencies_dict = dict(group_equivalencies_set)

for i in range(5):
    group_ids['group'].replace(equivalencies_dict, inplace=True)

# Write the group ids
group_ids.reset_index(inplace=True, drop=False)
grouped_df = record_df.merge(group_ids, how='left', on="RecordID")
grouped_df.to_csv("/ssd-1/clinical/clinical-abbreviations/data/Step3Output_with_group_fixed.csv", index=False)


# Do some testing for possible mistakes, not  necesary to recieve the group ID
def check_for_failure(grouped_df):
    fail_count = 0
    record_fails = []
    for ix, row in match_df.iterrows():
        id_1 = row["RecordID1"]
        id_2 = row["RecordID2"]
        a = grouped_df[grouped_df['RecordID'] == id_1][['group']].iloc[0, 0]
        b = grouped_df[grouped_df['RecordID'] == id_2][['group']].iloc[0, 0]
        if a != b:
            fail_count += 1
            record_fails.append([(id_1, id_2)])
            print('Failure')

    for pair in record_fails:
        id_1, id_2 = pair[0]
        a = grouped_df[grouped_df['RecordID'] == id_1][['group', 'SF','LF','LFEUI']]
        b = grouped_df[grouped_df['RecordID'] == id_2][['group', 'SF', 'LF', 'LFEUI']]
        print(a, b)

    b = grouped_df.groupby(['SF', 'LFEUI'])['group'].nunique().to_frame('size')
    print(b.max())
    b = grouped_df.groupby(['group'])['SF'].nunique().to_frame('size')
    print(b)
    return 1

#check_for_failure(grouped_df)