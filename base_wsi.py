#%%
from wsi.lm_bert import LMBert, trim_predictions
from wsi.WSISettings import DEFAULT_PARAMS, WSISettings
from wsi.wsi_clustering import cluster_predictions

from ast import literal_eval
from typing import List
from datetime import datetime
from dateutil import tz
from pathlib import Path
import pandas as pd
from glob import glob
import time

def get_data(target_path, sentence_path=None, corpus_name=None, 
             occurence_limit=None, length_minimum=25):
    if 'csv' in target_path:
        target_data = pd.read_csv(target_path)
    elif 'pkl' in target_path:
        target_data = pd.read_pickle(target_path)
    
    print(f'{len(target_data)} target instances pulled')

    if sentence_path:
        if 'csv' in sentence_path:
            sentence_data = pd.read_csv(sentence_path, usecols=['corpus', 'sent_id'])
            sentence_data.set_index(['sent_id'], inplace=True)
        elif 'pkl' in sentence_path:
            sentence_data = pd.read_pickle(sentence_path)
            sentence_data.drop(columns=['sentence', 'processed_sentence', 'word_index_sentence'], inplace=True)
        target_data = target_data.join(sentence_data, on='sent_id')

        if corpus_name is not None:
            target_data = target_data[target_data.corpus == corpus_name]
            print(f'{len(target_data)} instances within {corpus_name}')

    vc = target_data.target.value_counts()
    print(f'{len(vc)} targets before anything removed')
    targets = vc[vc >= 25].index
    target_data = target_data[target_data.target.isin(targets)]
    print(f'{len(target_data)} after insufficient targets removed')

    if length_minimum:
        ids = target_data[target_data.length <= 25].sent_id.unique()
        target_data = target_data[~target_data.sent_id.isin(ids)]
        print(f'{len(target_data)} after length minimum applied')

    if occurence_limit:
        vc = target_data.sent_id.value_counts()
        ids = vc[vc <= occurence_limit].index
        target_data = target_data[target_data.sent_id.isin(ids)]
        print(f'{len(target_data)} after limit applied')

    # target_data.formatted_sentence = target_data.formatted_sentence.apply(literal_eval)

    return target_data

def pull_rows(data, subset_num):
    target_rows = {}
    sent_ids = set()
    vc = data.target.value_counts(ascending=True)
    for target in vc.index:        
        # If the target is the only thing in the sent, we'll get nonsense. 
        ## TODO: issue if we disclude based on length here :/
        data_subset = data[(data.target == target) & (data.length >= 25)]
        print(target, len(data_subset))

        ## If too big, skip for now. 
        if subset_num is not None and (len(data_subset) > subset_num):
            continue
        else:
            ## Save the ids of all samples in case they overlap, 
            ## since one sentence can have multiple targets, we want to include them all.

            target_rows[target] = data_subset
            # before = len(sent_ids)
            sent_ids.update(data_subset.sent_id)  
            # print(f'\t{len(sent_ids) - before} ids added')

    ## Now we go back through and resample those that were too big
    ## now that all the samples have been accounted for.
    for target in vc.index:
        if target not in target_rows:
            print(target)

            data_subset = data[(data.target == target) & (data.length >= 25)]
            already_sampled = sum(data_subset.sent_id.isin(sent_ids))
            print(target)
            print(f'\t{already_sampled} already sampled')

            sample_subset = data_subset[~data_subset.sent_id.isin(sent_ids)]
            sample_num = max(0, subset_num - already_sampled)
            sent_ids.update(sample_subset.sample(sample_num).index)
            print(sample_num)

            target_rows[target] = data_subset[data_subset.sent_id.isin(sent_ids)]
            print(target, len(data_subset))

    return target_rows

def pull_corpus_rows(data, targets, subset_num):
    target_rows = {}
    for target in targets:
        # If the target is the only thing in the sent, we'll get nonsense. 
        data_subset = data[(data.target.isin(target)) & (data.length >= 25)]
        if subset_num:
            samples = []

            for corpus in data_subset.corpus.unique():
                c_subset = data_subset[data_subset.corpus == corpus]
                num_rows = min(len(c_subset), subset_num)
                c_subset = c_subset.sample(num_rows)
                samples.append(c_subset)

            data_subset = pd.concat(samples)
        
        target_rows[target[0]] = data_subset

    return target_rows

def convert_to_local(t):
    from_zone = tz.tzutc()
    to_zone = tz.tzlocal()

    t = datetime.utcfromtimestamp(t)
    t = t.replace(tzinfo=from_zone)
    t = t.astimezone(to_zone)

    return datetime.strftime(t, '%H:%M')

def make_predictions(
    data: pd.DataFrame,
    dataset_desc: str,
    output_path: str,
    targets: List[str],
    subset_num=None,
    resume_predicting=False
    ):

    ## Pull settings from file
    settings = DEFAULT_PARAMS._asdict()
    settings = WSISettings(**settings)

    ## Load base BERT model
    lm = LMBert(settings.cuda_device, settings.bert_model, settings.max_batch_size)

    Path(f'{output_path}/predictions').mkdir(parents=True, exist_ok=True)
    logging_file = f'{output_path}/prediction.log'

    ## Start the new logging file for this run
    if not resume_predicting:
        with open(logging_file, 'w') as flog:
            print(dataset_desc, file=flog)
            print(f'\n{len(data)} rows loaded', file=flog)
            print(f'{len(targets)} targets loaded\n', file=flog)
    else:
        already_predicted = glob(f'{output_path}/predictions/*.pkl')
        skip_targets = [path.split('/')[-1][:-4] for path in already_predicted]
        print(f'{len(skip_targets)} targets already predicted')

        remove_targets = []
        for target in targets:
            if target[0] in skip_targets:
                remove_targets.append(target)
        print(f'Removing {len(remove_targets)} targets')

        for target in remove_targets:
            targets.remove(target)
        print(f'{len(targets)} targets going to be clustered')

    target_rows = pull_rows(data, subset_num)

    for n, target_alts in enumerate(sorted(targets)):
        # break
        target = target_alts[0]
        print(f'\n{n+1} / {len(targets)} : {" ".join(target_alts)}')

        data_subset = target_rows[target]
        num_rows = len(data_subset)

        with open(logging_file, 'a') as flog:
            print('====================================\n', file=flog)
            print(f'{target.capitalize()} : {num_rows} rows', file=flog)
            if len(target_alts) > 1:
                print(f'Alt form: {target_alts[1]}', file=flog)

            print(f'\tPredicting for {num_rows} rows...')
            start = time.time()
            print(f'\t  Start time : {convert_to_local(start)}')
            print(f'\n\t  Start time : {convert_to_local(start)}', file=flog)
            predictions = lm.predict_sent_substitute_representatives(data_subset, settings)
            end = time.time()
            print(f'\t    End time : {convert_to_local(end)}\n', file=flog)
            print(f'\t    End time : {convert_to_local(end)}')
            
            predictions.to_pickle(f'{output_path}/predictions/{target}.pkl')
            print(f'\tPredictions saved')

#%%
def make_clusters(
    target_data: pd.DataFrame,
    dataset_desc: str,
    output_path: str,
    targets: List[str],
    resume_clustering: bool = False
    ):

    ## Pull settings from file
    settings = DEFAULT_PARAMS._asdict()
    settings = WSISettings(**settings)

    ## Only path that needs to be made
    Path(f'{output_path}/summaries').mkdir(parents=True, exist_ok=True)
    logging_file = f'{output_path}/clustering.log'

    ## Start the new logging file for this run
    if not resume_clustering:
        with open(logging_file, 'w') as flog:
            print(dataset_desc, file=flog)
    else:
        all_sense_data = pd.read_pickle(f'{output_path}/target_sense_labels.pkl')
        skip_targets = all_sense_data.target.unique()
        print(f'{len(skip_targets)} targets already clustered')
        
        remove_targets = []
        for target in targets:
            if target[0] in skip_targets:
                remove_targets.append(target)
        print(f'Removing {len(remove_targets)} targets')

        for target in remove_targets:
            targets.remove(target)

        print(f'{len(targets)} targets going to be clustered')

    sense_data = []
    for n, target_alts in enumerate(sorted(targets)):
        # break
        target = target_alts[0]
        print(f'\n{n+1} / {len(targets)} : {" ".join(target_alts)}')

        predictions = pd.read_pickle(f'{output_path}/predictions/{target}.pkl')
        # print(f'\tPredictions loaded')
        predictions = trim_predictions(predictions, target_alts)

        with open(logging_file, 'a') as flog:
            print('====================================\n', file=flog)
            print(f'{target.capitalize()} : {len(predictions)} rows', file=flog)
            if len(target_alts) > 1:
                print(f'Alt form: {target_alts[1]}', file=flog)

            if len(predictions) >= 100:
                # print('\n\tClustering likelihoods...')
                start = time.time()
                print(f'\t  Start time : {convert_to_local(start)}')
                print(f'\t  Start time : {convert_to_local(start)}', file=flog)
                sense_clusters, cluster_centers = cluster_predictions(predictions, settings)
                end = time.time()
                print(f'\t    End time : {convert_to_local(end)}', file=flog)
                print(f'\t    End time : {convert_to_local(end)}')
            else:
                ## We don't want to cluster a target that is too small
                print('\tSkipping WSI; not enough rows\n', file=flog)
                sense_clusters = {
                    '0' : list(predictions.index)
                }

            print('\n\tCluster results')
            for sense, cluster in sense_clusters.items():
                print(f'\t{sense} : {len(cluster)}', file=flog)
                print(f'\t{sense} : {len(cluster)}')

        sense_info = save_results(target_data, dataset_desc, target, sense_clusters, output_path)
        sense_data.append(sense_info)

    if len(sense_data) > 0:
        sense_data = pd.concat(sense_data)
        sense_data.set_index('word_index', inplace=True)
        
        if resume_clustering:
            sense_data = pd.concat([all_sense_data, sense_data])        
        
        sense_data.to_pickle(f'{output_path}/target_sense_labels.pkl')
# %%
def save_results(data, dataset_desc, target, sense_clusters, output_path):
    cluster_data = []
    for sense_label, indices in sense_clusters.items():
        filter = data.word_index.isin(indices)
        subset_indices = data[filter].index

        sense_subset = data.loc[subset_indices, ['word_index', 'target', 'sent_id']]
        sense_subset['cluster'] = sense_label

        cluster_data.append(sense_subset)
    
    cluster_data = pd.concat(cluster_data)

    with open(f'{output_path}/summaries/{target}.txt', 'w+') as fout:
        print(f'=================== {target.capitalize()} ===================\n', file=fout)
        print(f'{len(sense_clusters)} sense(s); {len(cluster_data)} sentences', file=fout)
        print(f'\nUsing data from {dataset_desc}', file=fout)

        for sense, cluster in sense_clusters.items():
            print(f'\n=================== Sense {sense} ===================', file=fout)
            print(f'{len(cluster)} sentences\n', file=fout)

            print('Example sentences', file=fout)
            data_rows = data[data.word_index.isin(cluster[:20])]
            for pre, targ, post in data_rows.formatted_sentence:
                print(f'\t{pre} *{targ}* {post}\n', file=fout)

    return cluster_data

def create_sense_sentences(sentence_path, output_path):
    target_data = pd.read_pickle(
        f'{output_path}/target_sense_labels.pkl')
    print(f'{len(target_data)} targets predicted')

    targets = list(target_data.target.unique())
    print(f'{len(targets)} targets selected')
    ids = target_data.sent_id.unique()
    print(f'{len(ids)} unique sentences')

    ## TODO: somehow there are terms that don't have a sent_id;
    ## how did that even happen?
    # good_target_data = pd.read_csv(target_path,
    #     usecols=['word_index', 'sent_id'])
    # good_target_data.set_index('word_index', inplace=True) ## maybe this default
    
    # target_data = target_data.join(good_target_data)
    # target_data.dropna(inplace=True)
    # target_data.sent_id = target_data.sent_id.astype(int)
    # print(f'{len(target_data)} targets after nulls removed')

    if 'csv' in sentence_path:
        sentence_data = pd.read_csv(sentence_path, usecols=['sent_id', 'word_index_sentence'])
        sentence_data.word_index_sentence = sentence_data.word_index_sentence.apply(eval)
        # sentence_data.set_index('sent_id', inplace=True) ## maybe this default
    elif 'pkl' in sentence_path:
        sentence_data = pd.read_pickle(sentence_path)
        sentence_data.drop( columns=['corpus','sentence', 'processed_sentence'],
                            inplace=True)

    good_ids = sentence_data.index.intersection(ids)
    if len(good_ids) < len(ids):
        bad_ids = set(ids) - set(good_ids)
        print(f'Removing {len(bad_ids)} sents from these targets:')
        print(target_data[target_data.sent_id.isin(bad_ids)].target.unique())
        ids = good_ids
    sentence_data = sentence_data.loc[ids]

    sense_sents = []
    num_bad = 0
    for sent_id, row in sentence_data.iterrows():
        sent = row['word_index_sentence']

        sense_sent = []
        add_sent = True
        for word in sent:
            target = word.split('.')[0]
            if '.' not in word:
                sense_sent.append(word)

            elif target not in targets:
                sense_sent.append(target)

            elif word in target_data.index:
                t_row = target_data.loc[word]
                sense = f'{target}.{t_row.cluster}'
                sense_sent.append(sense)

            else:
                print(f'Bad! {sent_id} - {word}')
                num_bad += 1
                add_sent = False
                break

        if add_sent:
            sense_sents.append([sent_id, sense_sent])

    print(f'{len(sense_sents)} sentences modified with senses')
    print(f'{num_bad} sentences were skipped')

    sense_data = pd.DataFrame(sense_sents, columns=['sent_id', 'sense_sentence'])
    sense_data.set_index('sent_id', inplace=True) 

    sense_data.to_pickle(f'{output_path}/sense_sentences.pkl')
