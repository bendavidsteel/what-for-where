import collections
import json
import os

import matplotlib.pyplot as plt
import pandas as pd

def plot_results(dir_path):
    with open(os.path.join(dir_path, 'parameters.json')) as f:
        params = json.load(f)
    with open(os.path.join(dir_path, 'results.json')) as f:
        results = json.load(f)

    df = pd.DataFrame(results)
    returns = [r['result'] for r in results]
    if not isinstance(returns[0], dict):
        returns = [{'pre_time': r['pre_time'], 'post_time': r['post_time'], 'return': r['result']} for r in results]
    result_df = pd.DataFrame(returns)
    result_df['pre_time'] = pd.to_datetime(result_df['pre_time'], format='mixed')
    result_df['post_time'] = pd.to_datetime(result_df['post_time'], format='mixed')
    
    df['num_exceptions'] = df['exceptions'].apply(len)
    result_timeline_df = result_df[['pre_time', 'return']].dropna().groupby(pd.Grouper(key='pre_time', freq='1s')).count()
    
    fig, axes = plt.subplots(nrows=1, ncols=3, figsize=(12, 5))
    axes[0].bar(df['num_exceptions'].value_counts().index, df['num_exceptions'].value_counts())
    axes[0].set_yscale('log')
    axes[0].set_xlabel('Number of exceptions')
    axes[0].set_ylabel('Number of occurrences')
    axes[0].set_title('Number of exceptions')
    axes[1].plot(result_timeline_df.index, result_timeline_df, label='Returns')
    axes[1].set_xlabel('Time')
    axes[1].set_ylabel('Number of fetches')
    axes[1].set_title('Fetches over time')

    exception_lists = [r['exceptions'] for r in results]
    exceptions = [e for l in exception_lists for e in l]
    if len(exceptions) > 0 and isinstance(exceptions[0], dict):
        exception_df = pd.DataFrame(exceptions)
        exception_df['pre_time'] = pd.to_datetime(exception_df['pre_time'], format='mixed')
        exception_df['post_time'] = pd.to_datetime(exception_df['post_time'], format='mixed')
        exception_timeline_df = exception_df[['pre_time', 'exception']].dropna().groupby(pd.Grouper(key='pre_time', freq='1s')).count()
        axes[1].plot(exception_timeline_df.index, exception_timeline_df, label='Exceptions')
        axes[1].legend()

    videos = []
    for r in results:
        if 'result' in r and r['result'] and 'id' in r['result']:
            videos.append(r['result'])
        elif 'result' in r and r['result'] and 'return' in r['result'] and r['result']['return'] and 'id' in r['result']['return']:
            videos.append(r['result']['return'])
    num_videos = len(videos)
    fargate_spot_usd_per_vcpu_per_hour = 0.013368
    fargate_spot_usd_per_gb_per_hour = 0.0014595
    num_seconds = (result_df['post_time'].max() - result_df['pre_time'].min()).total_seconds()
    num_hours = num_seconds / 3600
    num_vcpus = params['worker_cpu'] / 1024
    num_gb = params['worker_mem'] / 1024
    num_workers = params['num_workers']
    cost = num_workers * num_hours * (num_vcpus * fargate_spot_usd_per_vcpu_per_hour + num_gb * fargate_spot_usd_per_gb_per_hour)
    time_span = f"{params['num_time']}{params['time_unit']}"
    fig.suptitle(f"Time Interval: {time_span}, Number of fetches: {len(results)}, Number of videos: {num_videos}, Estimated Cost: ${cost:.2f}")

    def get_type(ex):
        ex_txt = ex['exception']
        if 'workers died while running it' in ex_txt:
            return 'Worker died'
        elif 'find normal JSON section' in ex_txt:
            return 'No JSON'
        else:
            return ex_txt
    exception_types = [get_type(ex) for ex in exceptions if 'exception' in ex]

    top_exceptions = [[e, count] for e, count in sorted(collections.Counter(exception_types).items(), key=lambda x: x[1], reverse=True)[:5]]
    axes[2].table(cellText=[ex for ex in top_exceptions], colLabels=['Exception', 'Number'])

    fig.savefig(os.path.join(dir_path, 'plot.png'))
    plt.close(fig)

    # get the number of unique bits in the ID
    ids = [r['id'] for r in videos]
    video_last_bits = [format(int(id), '064b')[41:] for id in ids]
    print(f"Number of unique bits in the ID: {len(set(video_last_bits))}")

def main():
    this_dir_path = os.path.dirname(os.path.realpath(__file__))
    data_dir_path = os.path.join(this_dir_path, '..', 'data', 'results')

    for root, dirs, files in os.walk(data_dir_path):
        if 'results.json' in files and 'parameters.json' in files:
            plot_results(root)

if __name__ == '__main__':
    main()