import os
import re

import datamapplot
import matplotlib as mpl
import numpy as np
import polars as pl
from PIL import Image

def convert_to_image(cols):
    return Image.frombytes(cols['Visual_Aspect_Mode'], tuple(cols['Visual_Aspect_Size']), cols['Visual_Aspect_Bytes'])

def main():
    this_dir_path = os.path.dirname(os.path.realpath(__file__))
    data_dir_path = os.path.join(this_dir_path, '..', 'data', f'topic_model_videos_1000')

    video_df = pl.read_parquet(os.path.join(data_dir_path, 'video_topics.parquet.gzip'))

    embeddings_2d = np.load(os.path.join(data_dir_path, 'reduced_embeddings.npy'))

    topic_info_df = pl.read_parquet(os.path.join(data_dir_path, 'topic_info.parquet.gzip'))
    # topic_info_df['Visual_Aspect'] = topic_info_df[['Visual_Aspect_Mode', 'Visual_Aspect_Size', 'Visual_Aspect_Bytes']].apply(convert_to_image, axis=1)

    topic_info_df = topic_info_df.with_columns(pl.col('Name').map_elements(lambda n: ','.join(n.split('_')[1:]), return_dtype=pl.String).alias('Desc'))

    top_n_topics = 30
    if top_n_topics:
        topic_info_df = topic_info_df.sort('Count', descending=True).head(top_n_topics)

    # Prepare text and names
    topic_name_mapping = {row['Topic']: row['Desc'] for row in topic_info_df[['Topic', 'Desc']].to_dicts()}
    topic_name_mapping[-1] = "Unlabelled"

    if top_n_topics:
        for topic_num in topic_info_df['Topic'].to_list():
            if topic_num not in topic_name_mapping:
                topic_name_mapping[topic_num] = "Unlabelled"

    # If a set of topics is chosen, set everything else to "Unlabelled"
    chosen_topics = None
    if chosen_topics:
        selected_topics = set(chosen_topics)
        for topic_num in topic_name_mapping:
            if topic_num not in selected_topics:
                topic_name_mapping[topic_num] = "Unlabelled"

    # Map in topic names and plot
    named_topic_per_doc = video_df['topic'].replace_strict(topic_name_mapping, default='Unlabelled').to_list()

    # TODO dot size determined by view count

    fig, axes = datamapplot.create_plot(
        embeddings_2d,
        labels=named_topic_per_doc,
        label_over_points=True,
        dynamic_label_size=True,
        # dynamic_label_size_scaling_factor=0.75,
        min_font_size=8.0,
        max_font_size=16.0,
        dpi=300,
        marker_size_array=video_df['playCount'].to_numpy(),
    )

    axes.set_axis_off()
    fig.subplots_adjust(left=0, bottom=0, right=1, top=1, wspace=0, hspace=0)

    figs_dir_path = os.path.join(this_dir_path, '..', 'figs')
    os.makedirs(figs_dir_path, exist_ok=True)
    fig.savefig(os.path.join(figs_dir_path, 'datamapplot.png'), bbox_inches='tight')

if __name__ == '__main__':
    main()