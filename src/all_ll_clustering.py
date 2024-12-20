from sentence_transformers import SentenceTransformer

import umap
import hdbscan
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import os
from tqdm import tqdm
from utils import *
import argparse


sentence_embed_model = SentenceTransformer("all-mpnet-base-v2")
embed_save_path = ""
batch_size = 32

# https://umap-learn.readthedocs.io/en/latest/parameters.html
umap_params = {
    'n_neighbors': 15,
    'min_dist': 0.0,
    'n_components': 10,  # final reduced dimensionality
    'metric': 'euclidean',
    'random_state': 42,
}

# https://hdbscan.readthedocs.io/en/latest/parameter_selection.html
hdbscan_params = {
    'min_cluster_size': 15,
    'min_samples': 1,
    'approx_min_span_tree': True,  # faster, but use False for more accuracy
    'cluster_selection_method': 'leaf',
    'allow_single_cluster': True,
    'memory': '../cache/',
}


class ClustersObj():
    def __init__(self, labels, probs):
        self.labels_ = labels
        self.probabilities_ = probs


def concat(main_emb, new_emb):
    if main_emb is not None:
        return np.vstack([main_emb, new_emb])
    return new_emb


def create_embeddings(all_ll_list):
    if os.path.exists(embed_save_path + ".npy"):
        print("loading pre-computed embeddings")
        return np.load(embed_save_path + ".npy")

    print("Start low-level embedding")
    all_embeddings = None
    for i in tqdm(range(0, len(all_ll_list), batch_size)):
        batch = all_ll_list[i:i + batch_size]
        embeddings = sentence_embed_model.encode(batch)
        all_embeddings = concat(all_embeddings, embeddings)
    np.save(embed_save_path, all_embeddings)
    return all_embeddings


def load_ll():
    filepath = "../tat/tat_ll"
    with open(filepath, 'r') as f:
        text = f.read()
    return text.split('\n')


def get_dr_embeddings(embeddings, umap_params, saved_path, run_new_cluster):
    umap_path = saved_path + "umap_embeds.npy"
    if os.path.exists(umap_path) and not run_new_cluster:
        print("loading pre-computed embeddings from " + umap_path)
        umap_embeds = np.load(umap_path)
        return umap_embeds

    dr_model = umap.UMAP(**umap_params)
    umap_embeds = dr_model.fit_transform(embeddings)
    return umap_embeds


def generate_clusters(umap_embeds, hdbscan_params, saved_path, run_new_cluster):
    cluster_label_path = saved_path + "cluster_labels.npy"
    if os.path.exists(cluster_label_path) and not run_new_cluster:
        print("loading pre-computed embeddings from " + cluster_label_path)
        clusters_labels = np.load(cluster_label_path)
        clusters_probs = np.load(saved_path + "cluster_probs.npy")
        return ClustersObj(clusters_labels, clusters_probs)

    cluster_model = hdbscan.HDBSCAN(**hdbscan_params)
    clusters = cluster_model.fit(umap_embeds)
    return clusters


def score_clusters(clusters, prob_threshold=0.05):
    cluster_labels = clusters.labels_
    label_count = len(np.unique(cluster_labels))
    total_num = len(cluster_labels)
    cost = (np.count_nonzero(clusters.probabilities_ < prob_threshold) / total_num)

    penalty = 0

    # want around 10k good clusters +/- a few singleton clusters
    if (label_count < 11000) | (label_count > 30000):
        penalty += 0.15

    # want at least 10k good clusters
    series_numbers = pd.Series(cluster_labels)
    all_occurrences = series_numbers.value_counts()
    unclustered = all_occurrences[-1]
    clean_series = series_numbers[series_numbers != -1]
    occurrences = clean_series.value_counts()
    plt.hist(occurrences.values, bins = 40)
    plt.xlabel("Cluster Size")
    plt.ylabel("Frequency")
    plt.title("Cluster size distribution")
    plt.savefig(f'../out/{run_name}/cluster_size_distribution.png')
    count_cluster_too_large = len(occurrences[occurrences > 20])
    if unclustered > 5000:
        penalty += (0.15 * unclustered//5000)
    if count_cluster_too_large:
        penalty += count_cluster_too_large * 0.5

    print("Cluster stats:")
    print("Unclustered occurrences", all_occurrences[-1])
    print("Total clusters:", label_count)
    print("Cluster size > 20: ", occurrences[occurrences > 20].index.tolist())
    print("Biggest Cluster Size", occurrences.max())
    print("Total cost:", cost + penalty)
    print()

    return cost + penalty


def run(write_obj, umap_params, hdbscan_params, custom_saved_path="", score_cluster=True, run_new_cluster=False):
    all_ll_list = load_ll()
    print(f"loaded all {len(all_ll_list)} claims")

    embeddings = create_embeddings(all_ll_list)

    write_obj.write_params({"umap_params": umap_params, "hdbscan_params": hdbscan_params})

    if custom_saved_path:
        saved_path = custom_saved_path
    else:
        saved_path = write_obj.out_dir

    umap_embeds = get_dr_embeddings(embeddings, umap_params, saved_path, run_new_cluster)
    write_obj.write_embeds(umap_embeds, "umap")

    clusters = generate_clusters(umap_embeds, hdbscan_params, saved_path, run_new_cluster)

    if score_cluster:
        print("writing clusters")
        clusters.labels_ = clusters.labels_[1:]
        clusters.probabilities_ = clusters.probabilities_[1:]
        write_obj.write_cluster(clusters, all_ll_list[1:])  # Remove first elem since it's "Found low level hypotheses:"

        score_clusters(clusters)

    return clusters, all_ll_list


def get_clustered_lls(write_obj, umap_params, hdbscan_params, custom_saved_path="", score_cluster=True, run_new_cluster=False):

    clusters, all_lls = run(write_obj, umap_params, hdbscan_params, custom_saved_path, score_cluster, run_new_cluster)
    clusters_list = list(zip(clusters.labels_, clusters.probabilities_))

    final_result = {}
    for i, elem in enumerate(list(zip(clusters_list[1:], all_lls[1:]))):  # Remove first elem since it's Found low level hypotheses:
        cluster, ll = elem
        cluster_label, cluster_prob = cluster
        cluster_label = int(cluster_label)
        if cluster_label == -1:
            continue
        if cluster_label not in final_result:
            final_result[cluster_label] = []
        ll_obj = {"ll_id": i, "ll": ll, "cluster_prob": cluster_prob}
        final_result[cluster_label].append(ll_obj)
    return final_result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Low-level clustering input.")
    parser.add_argument('-r', '--run_name', type=str, required=True,
                        help='The name of the run, which will define the output path where logged results are saved.')
    parser.add_argument('-e', '--embed_save_path', type=str, default="../tat/sentence_embeddings",
                        help='A path to pre-computed sentence embeddings for each low-level claims or, if not available, where the computed embeddings will be saved.')
    parser.add_argument('-s', '--custom_saved_path', type=str,
                        help='Custom path to load or save the UMAP and HDBSCAN cluster results. If not provided, results will be loaded from or saved in out/run_name.')
    parser.add_argument('-c', '--run_new_cluster', type=bool, default=False,
                        help='Force running new clusters instead of loading pre-computed clusters.')

    args = parser.parse_args()

    run_name = args.run_name
    embed_save_path = args.embed_save_path
    custom_saved_path = args.custom_saved_path
    run_new_cluster = args.run_new_cluster

    write_obj = OutputWriter(run_name, log=True)
    result_map = get_clustered_lls(write_obj, umap_params, hdbscan_params, custom_saved_path, run_new_cluster)
    f = open("../out/" + run_name + "/final_cluster_map.json", "w")
    json.dump(result_map, f, indent=4)
    f.close()
    
"""
Overriding existing run outputs at run name: cluster_v1_full. Confirm: y/n
y
loaded all 64099 claims
loading pre-computed embeddings
loading pre-computed embeddings from ../out/cluster_v1_full/umap_embeds.npy
loading pre-computed embeddings from ../out/cluster_v1_full/cluster_labels.npy
writing clusters
Cluster stats:
Unclustered occurrences 28097
Total clusters: 6479
Cluster size > 20:  [3739, 3862, 3092, 3291, 6347, 4115, 1992, 205, 4677, 3738, 2361, 1367, 900, 1870, 99, 2194, 2803, 4242, 5630, 2855, 2092, 2579, 6054, 1019, 3888, 2280, 5486, 5246, 2415, 4520, 4245, 2189, 3162, 3276, 1439, 5330, 3584, 1572, 6222, 3333, 2335, 3160, 3128, 4480]
Total cost: 22.58930482534829
"""