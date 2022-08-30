#%%
from wsi.lm_bert import LMBert, trim_predictions
from wsi.WSISettings import DEFAULT_PARAMS, WSISettings
from wsi.wsi_clustering import cluster_predictions, find_best_sents, get_cluster_centers
from typing import List, Dict
from datetime import datetime
from dateutil import tz
from pathlib import Path
import pandas as pd
from glob import glob
import pickle
import time

def convert_to_local(t):
    from_zone = tz.tzutc()
    to_zone = tz.tzlocal()

    t = datetime.utcfromtimestamp(t)
    t = t.replace(tzinfo=from_zone)
    t = t.astimezone(to_zone)

    return datetime.strftime(t, '%H:%M')

def record_time(desc):
    t = convert_to_local(time.time())
    t_str = f'\t  {desc.capitalize()} time : {t}'
    print(t_str)
    
    return t_str

def make_predictions(
    target_data: pd.DataFrame,
    targets: List[str],
    dataset_desc: str,
    output_path: str,
    resume_predicting=False,
    embed_sents=False
    ):

    settings = DEFAULT_PARAMS._asdict()
    settings = WSISettings(**settings)

    ## Load base BERT model
    lm = LMBert(settings)

    if embed_sents:
        Path(f'{output_path}/vectors').mkdir(parents=True, exist_ok=True)
    else:
        Path(f'{output_path}/predictions').mkdir(parents=True, exist_ok=True)
    logging_file = f'{output_path}/prediction.log'

    ## Start the new logging file for this run
    if not resume_predicting:
        with open(logging_file, 'w') as flog:
            print(dataset_desc, file=flog)
            # print(f'\n{len(target_data):,} rows loaded', file=flog)
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

    for n, target_alts in enumerate(sorted(targets)):
        # break
        target = target_alts[0]
        print(f'\n{n+1} / {len(targets)} : {" ".join(target_alts)}')

        data_subset = target_data[target_data.target == target]
        num_rows = len(data_subset)

        with open(logging_file, 'a') as flog:
            print('====================================\n', file=flog)
            print(f'{target.capitalize()} : {num_rows} rows', file=flog)
            if len(target_alts) > 1:
                print(f'Alt form: {target_alts[1]}', file=flog)

            print(f'\tPredicting for {num_rows} rows...')
            print('\n' + record_time('start'), file=flog)
            if embed_sents:
                vectors = lm.embed_sents(data_subset, target_alts[-1])
                print(record_time('end') + '\n', file=flog)

                with open(f'{output_path}/vectors/{target}.pkl', 'wb') as vp:
                    pickle.dump(vectors, vp, protocol=pickle.HIGHEST_PROTOCOL)
                print(f'\tVectors saved')

            else:
                predictions = lm.predict_sent_substitute_representatives(
                    data_subset, settings, target_alts[-1])
                print(record_time('end') + '\n', file=flog)
                
                predictions.to_pickle(f'{output_path}/predictions/{target}.pkl')
                print(f'\tPredictions saved')

#%%

def get_cluster_data(sense_clusters, target_data):
    cluster_data = []
    for sense_label, subset_indices in sense_clusters.items():
        sense_subset = target_data.loc[subset_indices, ['target', 'sent_idx']]
        sense_subset['cluster'] = sense_label        
        cluster_data.append(sense_subset)
    return pd.concat(cluster_data)

def prep_io(targets, output_path, plot_clusters, print_clusters,
     resume_clustering, dataset_desc):
    settings = DEFAULT_PARAMS._asdict()
    settings = WSISettings(**settings)

    logging_file = f'{output_path}/clustering.log'
    Path(f'{output_path}/summaries').mkdir(parents=True, exist_ok=True)
    Path(f'{output_path}/clusters').mkdir(parents=True, exist_ok=True)
    if plot_clusters:
        Path(f'{output_path}/clusters/plots').mkdir(parents=True, exist_ok=True)
    if print_clusters:
        Path(f'{output_path}/clusters/info').mkdir(parents=True, exist_ok=True)

    ## Start the new logging file for this run
    if not resume_clustering:
        with open(logging_file, 'w') as flog:
            print(dataset_desc, file=flog)
        all_sense_data = None
    ## TODO: should I be saving sense sents at every step?
    ## Otherwise this isn't saving any data incrementally
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

    return settings, logging_file, all_sense_data

def make_clusters(
    target_data: pd.DataFrame,
    targets: List[str],
    dataset_desc: str,
    min_sense_size: int,
    output_path: str,
    embed_sents=False,
    method='flat',
    resume_clustering: bool = False,
    plot_clusters: bool = False,
    print_clusters: bool = False
    ):

    settings, logging_file, all_sense_data = prep_io(
        targets, output_path, plot_clusters, print_clusters, 
        resume_clustering, dataset_desc)

    sense_data = []
    for n, target_alts in enumerate(sorted(targets)):
        # break
        target = target_alts[0]
        print(f'\n{n+1} / {len(targets)} : {" ".join(target_alts)}')

        ### Get vectors
        if embed_sents:
            with open(f'{output_path}/vectors/{target}.pkl', 'rb') as vp:
                predictions = pickle.load(vp)
                predictions = pd.DataFrame.from_dict(predictions).T
        else:
            predictions = pd.read_pickle(f'{output_path}/predictions/{target}.pkl')
            # print(f'\tPredictions loaded')
            subset = trim_predictions(predictions, target_alts, settings.language)
            predictions = predictions[subset]

        ### Do clustering
        use_clustering = len(predictions) >= (min_sense_size * 2) + 25
        if use_clustering:
            # print('\n\tClustering likelihoods...')            
            record_time('start')
            sense_clusters, cluster_centers = cluster_predictions(
                predictions, target_alts, settings, min_sense_size,
                plot_clusters, print_clusters, f'{output_path}/clusters')
            record_time('end')
        else:
            ## We don't cluster a target that is too small
            sense_clusters = {0 : list(predictions.index)}
            cluster_centers = get_cluster_centers(predictions, 1, sense_clusters)   

        if sense_clusters == None:
            continue

        with open(logging_file, 'a') as flog:
            print('====================================\n', file=flog)
            print(f'{target.capitalize()} : {len(predictions)} rows', file=flog)
            if len(target_alts) > 1:
                print(f'Alt form: {target_alts[1]}', file=flog)
            if not use_clustering:
                ## We don't want to cluster a target that is too small
                print('\tSkipping WSI; not enough rows\n', file=flog)

            print('\n\tCluster results')
            for sense, cluster in sense_clusters.items():
                print(f'\t{sense} : {len(cluster)}', file=flog)
                print(f'\t{sense} : {len(cluster)}')

        sense_data.append(get_cluster_data(sense_clusters, target_data))

        best_sentences = find_best_sents(target_data, predictions, cluster_centers, sense_clusters)
        save_results( dataset_desc, target, 
                      sense_clusters, best_sentences, len(predictions), output_path)

        center_path = f'{output_path}/clusters/{target}.csv'
        centers = pd.DataFrame(cluster_centers, columns=predictions.columns)
        centers.to_csv(center_path)

    if len(sense_data) > 0:
        sense_data = pd.concat(sense_data)
        if resume_clustering:
            sense_data = pd.concat([all_sense_data, sense_data])        
        
        sense_data.to_pickle(f'{output_path}/target_sense_labels.pkl')
    else:
        print('Error; nothing was generated')
# %%
def save_results(
    dataset_desc, target, sense_clusters,
    best_sentences, sentence_count, output_path):
    
    with open(f'{output_path}/summaries/{target}.txt', 'w+') as fout:
        print(f'=================== {target.capitalize()} ===================\n', file=fout)
        print(f'{len(best_sentences)} sense(s)', file=fout)
        print(f'{sentence_count} sentences', file=fout)
        print(f'\nUsing data from {dataset_desc}', file=fout)

        for sense, cluster in best_sentences.items():
            print(f'\n=================== Sense {sense} ===================', file=fout)
            print(f'{len(sense_clusters[sense])} sentences\n', file=fout)

            print('Central most sentences', file=fout)
            for index, (pre, targ, post) in cluster:
                print(f'\t{index}', file=fout)
                print(f'\t\t{pre} *{targ}* {post}\n', file=fout)

# %%
