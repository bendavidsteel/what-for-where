import asyncio
import datetime
import itertools
import json
import multiprocessing
import os
import random
import time
import traceback
from typing import Callable, Coroutine, Iterable, List, Optional

from dask.distributed import Client as DaskClient
from dask.distributed import LocalCluster as DaskLocalCluster
from distributed.scheduler import KilledWorker
from dask_cloudprovider.aws import FargateCluster as DaskFargateCluster
import dotenv
import httpx
from tqdm.asyncio import tqdm as atqdm
from tqdm import tqdm


async def aworker(
    coroutine: Coroutine,
    tasks_queue: asyncio.Queue,
    result_queue: asyncio.Queue,
    stop_event: asyncio.Event,
    timeout: float = 1,
    callback: Optional[Callable] = None
) -> None:
    """
    A worker coroutine to process tasks from a queue.

    Args:
        coroutine: The coroutine to be applied to each task.
        tasks_queue: The queue containing the tasks to be processed.
        result_queue: The queue to put the results of each processed task.
        stop_event: An event to signal when all tasks have been added to the tasks queue.
        timeout: The timeout value for getting a task from the tasks queue.
        callback: A function that can be called at the end of each coroutine.
    """
    # Continue looping until stop_event is set and the tasks queue is empty
    while not stop_event.is_set() or not tasks_queue.empty():
        try:
            # Try to get a task from the tasks queue with a timeout
            idx, arg = await asyncio.wait_for(tasks_queue.get(), timeout)
        except asyncio.TimeoutError:
            # If no task is available, continue the loop
            continue
        try:
            # Try to execute the coroutine with the argument from the task
            result = await coroutine(arg)
            # If successful, add the result to the result queue
            result_queue.put_nowait((idx, result))

        finally:
            # Mark the task as done in the tasks queue
            tasks_queue.task_done()
            # callback for progress update
            if callback is not None:
                callback(idx, arg)


async def amap(
    coroutine: Coroutine,
    data: Iterable,
    max_concurrent_tasks: int = 10,
    max_queue_size: int = -1,  # infinite
    callback: Optional[Callable] = None,
) -> List:
    """
    An async function to map a coroutine over a list of arguments.

    Args:
        coroutine: The coroutine to be applied to each argument.
        data: The list of arguments to be passed to the coroutine.
        max_concurrent_tasks: The maximum number of concurrent tasks.
        max_queue_size: The maximum number of tasks in the workers queue.
        callback: A function to be called at the end of each coroutine.
    """
    # Initialize the tasks queue and results queue
    # The queue size is infinite if max_queue_size is 0 or less.
    # Setting it to finite number will save some resources,
    # but will risk that an exception will be thrown too late.
    # Should be higher than the max_concurrent_tasks.
    tasks_queue = asyncio.Queue(max_queue_size)
    result_queue = asyncio.PriorityQueue()

    # Create an event to signal when all tasks have been added to the tasks queue
    stop_event = asyncio.Event()
    # Create workers
    workers = [
        asyncio.create_task(aworker(
            coroutine, tasks_queue, result_queue, stop_event, callback=callback
        ))
        for _ in range(max_concurrent_tasks)
    ]

    # Add inputs to the tasks queue
    for arg in enumerate(data):
        await tasks_queue.put(arg)
    # Set the stop_event to signal that all tasks have been added to the tasks queue
    stop_event.set()

    # Wait for all workers to complete
    # raise the earliest exception raised by a coroutine (if any)
    await asyncio.gather(*workers)
    # Ensure all tasks have been processed
    await tasks_queue.join()

    # Gather all results
    results = []
    while not result_queue.empty():
        # Get the result from the results queue and discard the index
        # Given that the results queue is a PriorityQueue, the index
        # plays a role to ensure that the results are in the same order
        # like the original list.
        _, res = result_queue.get_nowait()
        results.append(res)
    return results

async def async_map(coroutine, data, num_workers=10):
    pbar = atqdm(total=len(data))  # track progress tqdm

    def callback(*_):
        pbar.update()

    res = await amap(coroutine, data, num_workers, callback=callback)
    pbar.close()
    return res

def pool_map(function, data):
    with multiprocessing.Pool(10) as pool:
        res = list(tqdm(pool.imap(function, data), total=len(data)))  # track progress tqdm
    pool.join()
    return res

def wait_until(condition, interval=0.1, timeout=1, *args):
    start = time.time()
    while not condition(*args) and time.time() - start < timeout:
        time.sleep(interval)
    if time.time() - start >= timeout:
        raise TimeoutError("Timed out waiting for condition")

class DaskCluster:
    def __init__(self, cluster_type):
        if cluster_type == 'fargate':
            self.cluster = DaskFargateCluster(
                fargate_spot=True,
                image="daskdev/dask:latest-py3.10", 
                environment={'EXTRA_PIP_PACKAGES': 'httpx==0.27.0 tqdm==4.66.2 lz4==4.3.3 msgpack==1.0.8 toolz==0.12.1'},
                worker_cpu=256,
                worker_nthreads=1,
                worker_mem=512,
                n_workers=0,
                aws_access_key_id=os.environ['AWS_ACCESS_KEY'],
                aws_secret_access_key=os.environ['AWS_SECRET_KEY']
            )
        elif cluster_type == 'local':
            self.cluster = DaskLocalCluster()

    def __enter__(self):
        return self.cluster
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.cluster is not None:
            self.cluster.close()

def dask_map(function, args, num_workers=16, reqs_per_ip=1000, max_task_tries=3, max_tries=3):
    function = DaskFunc(function).func_wrapper
    tasks = [DaskTask(arg) for arg in args]
    dotenv.load_dotenv()
    batch_size = num_workers * reqs_per_ip
    num_tries = 0
    tasks_progress_bar = tqdm(total=len(tasks), desc="All Tasks")
    batch_progress_bar = tqdm(total=batch_size, desc="Batch Tasks")
    with DaskCluster('fargate') as cluster:
        with DaskClient(cluster) as client:
            num_left = len([t for t in tasks if not t.completed])
            while num_left > 0:
                try:
                    if hasattr(cluster, 'adapt'):
                        cluster.adapt(minimum=1, maximum=num_workers)
                        # wait for workers to start
                        wait_until(lambda: len(client.scheduler_info()["workers"]) > 0, timeout=120) 
                    batch_tasks = [t for t in tasks if not t.completed][:batch_size]
                    batch_args = [t.args for t in batch_tasks]
                    batch_progress_bar.reset(total=batch_size)
                    task_futures = client.map(function, batch_args)
                    for f in task_futures:
                        f.add_done_callback(lambda x: batch_progress_bar.update(1))
                    batch_result = client.gather(task_futures)

                    # sort out task returns, either they complete, 
                    # or had exceptions (which we want to keep track of), and need to be tried, or we give up
                    for t, r in zip(batch_tasks, batch_result):
                        if r['exception'] is not None:
                            t.exceptions.append(r['exception'])
                            if len(t.exceptions) >= max_task_tries:
                                t.completed = True
                                tasks_progress_bar.update(1)
                        else:
                            t.result = r
                            t.completed = True
                            tasks_progress_bar.update(1)
                    if hasattr(cluster, 'scale'):
                        # recreate workers to get new IPs
                        cluster.scale(0)
                        wait_until(lambda: len(client.scheduler_info()["workers"]) == 0, timeout=120) 
                    num_left = len([t for t in tasks if not t.completed])
                except KilledWorker as e:
                    pass
                except Exception as e:
                    print(f"Num Tries: {num_tries}, Error: {e}, Stacktrace: {traceback.format_exc()}")
                    continue
    tasks_progress_bar.close()
    batch_progress_bar.close()

    return tasks

class InvalidResponseException(Exception):
    pass

class NotFoundException(Exception):
    pass

def get_headers():
    headers = {
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/121.0.0.0 Safari/537.36',
        'sec-ch-ua': '"Chromium";v="121", "Not A(Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"'
    }
    return headers

def process_response(r):
    if r.status_code != 200:
        raise InvalidResponseException(
            r.text, f"TikTok returned a {r.status_code} status code."
        )

    # Try SIGI_STATE first
    # extract tag <script id="SIGI_STATE" type="application/json">{..}</script>
    # extract json in the middle

    # start = r.text.find('<script id="SIGI_STATE" type="application/json">')
    # if start != -1:
    #     start += len('<script id="SIGI_STATE" type="application/json">')
    #     end = r.text.find("</script>", start)

    #     if end == -1:
    #         raise InvalidResponseException(
    #             r.text, "TikTok returned an invalid response.", error_code=r.status_code
    #         )

    #     data = json.loads(r.text[start:end])
    #     video_info = data["ItemModule"][self.id]
    # else:
    # Try __UNIVERSAL_DATA_FOR_REHYDRATION__ next

    # extract tag <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{..}</script>
    # extract json in the middle

    start = r.text.find('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">')
    if start == -1:
        raise InvalidResponseException(
            r.text, "Could not find normal JSON section in returned HTML."
        )

    start += len('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">')
    end = r.text.find("</script>", start)

    if end == -1:
        raise InvalidResponseException(
            r.text, "Could not find normal JSON section in returned HTML."
        )

    data = json.loads(r.text[start:end])
    default_scope = data.get("__DEFAULT_SCOPE__", {})
    video_detail = default_scope.get("webapp.video-detail", {})
    if video_detail.get("statusCode", 0) != 0: # assume 0 if not present
        # TODO move this further up to optimize for fast fail
        if video_detail.get("statusCode", 0) == 10204:
            return None
        else:
            raise InvalidResponseException(
                r.text, "TikTok JSON had an unrecognised status code."
            )
    video_info = video_detail.get("itemInfo", {}).get("itemStruct")
    if video_info is None:
        raise InvalidResponseException(
            r.text, "TikTok JSON did not contain expected JSON."
        )
        
    return video_info

async def async_get_video(url):
    headers = get_headers()

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
    except Exception:
        raise InvalidResponseException(
            "TikTok returned an invalid response."
        )
    
    return process_response(r)

def get_video(video_id):
    url = f"https://www.tiktok.com/@therock/video/{video_id}"
    headers = get_headers()

    try:
        with httpx.Client() as client:
            r = client.get(url, headers=headers)
    except Exception:
        raise InvalidResponseException(
            "TikTok returned an invalid response."
        )
    
    return process_response(r)

class DaskFunc:
    def __init__(self, func):
        self.func = func

    def func_wrapper(self, arg):
        
        pre_time = datetime.datetime.now()
        try:
            res = self.func(arg)
            exception = None
        except Exception as e:
            res = None
            exception = e
        post_time = datetime.datetime.now()

        return {
            'res': res,
            'exception': exception,
            'pre_time': pre_time,
            'post_time': post_time,
        }

class DaskTask:
    def __init__(self, args):
        self.args = args
        self.exceptions = []
        self.result = None
        self.completed = False
    

def test_1_month_ago(i):
    timestamp_1year_time = int((datetime.datetime.now() - datetime.timedelta(days=30)).timestamp())
    # convert to binary
    timestamp_binary = format(timestamp_1year_time, '032b')
    # create random 32 bit number
    random_32bit = format(random.getrandbits(32), '032b')
    # concatenate into 64 bit number
    random_video_id = int(timestamp_binary + random_32bit, 2)
    does_exist = is_existing_video(random_video_id)
    if does_exist == 'exists':
        return 1, 1, None
    elif does_exist == 'invalid response':
        return 0, 0, None
    else:
        video = get_existing_video(random_video_id)
        return 0, 1, video

def test_1_year_ahead(i):
    timestamp_1year_time = int((datetime.datetime.now() + datetime.timedelta(days=365)).timestamp())
    # convert to binary
    timestamp_binary = format(timestamp_1year_time, '032b')
    # create random 32 bit number
    random_32bit = format(random.getrandbits(32), '032b')
    # concatenate into 64 bit number
    random_video_id = int(timestamp_binary + random_32bit, 2)
    does_exist = is_existing_video(random_video_id)
    if does_exist == 'exists':
        return 1, 1, None
    elif does_exist == 'invalid response':
        return 0, 0, None
    else:
        video = get_existing_video(random_video_id)
        return 0, 1, video

def iterate_binary(b):
    pass

def main():
    this_dir_path = os.path.dirname(os.path.realpath(__file__))
    # if False:
        # test real
        
        # data_dir_path = os.path.join(this_dir_path, "..", "data", "germany")
        # with open(os.path.join(data_dir_path, 'videos', 'all_010324.json'), 'r') as file:
        #     videos = json.load(file)

        # num_test = 1000

        # r = await async_map(test_real_video, [videos[i]['id'] for i in range(num_test)])
        # score = sum([x[0] for x in r if x[1] == 1])
        # num_valid = sum([x[1] for x in r])

        # assert score == num_valid
        # print(f"Score for real video IDs: {score / num_valid}")
        # print(f"Number of valid responses: {num_valid / num_test}")

        # # # test random 1 month ago timestamps
        # r = await async_map(test_1_month_ago, range(num_test))
        # score = sum([x[0] for x in r if x[1] == 1])
        # num_valid = sum([x[1] for x in r])

        # print(f"Score for real video IDs 1 month ago: {score / num_valid}")
        # print(f"Number of valid responses: {num_valid / num_test}")

        # # test random 1 year ahead timestamps
        # r = await async_map(test_1_year_ahead, range(num_test))
        # score = sum([x[0] for x in r if x[1] == 1])
        # num_valid = sum([x[1] for x in r])

        # print(f"Score for real video IDs 1 year ahead: {score / num_valid}")
        # print(f"Number of valid responses: {num_valid / num_test}")

    if True:
        with open(os.path.join(this_dir_path, '..', 'figs', 'all_videos', 'all_found_segments_combinations.json'), 'r') as file:
            data = json.load(file)

        # get bits of non timestamp sections of ID
        # order dict according to interval
        data = [(tuple(map(int, interval.strip('()').split(', '))), vals) for interval, vals in data.items()]
        data = sorted(data, key=lambda x: x[0][0])
        # get rid of millisecond bits
        data = [t for t in data if t[0] != (0,9)]
        interval_bits = []
        for interval, vals in data:
            # format ints to binary
            num_bits = interval[1] - interval[0] + 1
            bits = [format(i, f'0{num_bits}b') for i in vals]
            interval_bits.append(bits)
        other_bit_sequences = itertools.product(*interval_bits)
        other_bit_sequences = [''.join(bits) for bits in other_bit_sequences]

        # get all videos in 1 millisecond
        num_time = 100
        time_unit = 'ms'
        unit_map = {
            'ms': 'milliseconds',
            's': 'seconds',
        }
        time_delta = datetime.timedelta(**{unit_map[time_unit]: num_time})
        start_time = datetime.datetime(2024, 3, 1, 20, 0, 0)
        end_time = start_time + time_delta
        start_timestamp = start_time.timestamp()
        end_timestamp = end_time.timestamp()
        c_time = start_timestamp
        all_timestamp_bits = []
        while c_time < end_timestamp:
            unix_timestamp_bits = format(int(c_time), '032b')
            milliseconds = int(format(c_time, '.3f').split('.')[1])
            milliseconds_bits = format(milliseconds, '010b')
            timestamp_bits = unix_timestamp_bits + milliseconds_bits
            all_timestamp_bits.append(timestamp_bits)
            c_time += 0.001

        potential_video_bits = itertools.product(all_timestamp_bits, other_bit_sequences)
        potential_video_bits = [''.join(bits) for bits in potential_video_bits]
        potential_video_ids = [int(bits, 2) for bits in potential_video_bits]
        num_workers = 64
        reqs_per_ip = 100
        # r = await async_map(test_real_video, potential_video_ids, num_workers=64)
        results = dask_map(get_video, potential_video_ids, num_workers=num_workers, reqs_per_ip=reqs_per_ip)
        num_hits = len([r for r in results if r.result and r.result['res'] is not None])
        num_valid = len([r for r in results if r.completed])
        print(f"Num hits: {num_hits}, Num valid: {num_valid}, Num potential video IDs: {len(potential_video_ids)}")
        print(f"Fraction hits: {num_hits / num_valid}")
        print(f"Fraction valid: {num_valid / len(potential_video_ids)}")
        # convert to jsonable format
        json_results = [
            {
                'args': r.args, 
                'exceptions': [str(e) for e in r.exceptions], 
                'result': r.result['res'] if r.result is not None else None,
                'pre_time': r.result['pre_time'].isoformat() if r.result is not None else None,
                'post_time': r.result['post_time'].isoformat() if r.result is not None else None, 
                'completed': r.completed
            }
            for r in results
        ]

        results_dir_path = os.path.join(this_dir_path, '..', 'data', 'results')
        results_dirs = [dir_name for dir_name in os.listdir(results_dir_path)]

        with open(os.path.join(this_dir_path, '..', 'data', f'{num_time}{time_unit}_nw{num_workers}_rpip{reqs_per_ip}_potential_video_ids.json'), 'w') as f:
            json.dump(json_results, f)

if __name__ == '__main__':
    main()