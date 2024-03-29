import asyncio
from concurrent.futures import ThreadPoolExecutor
import datetime
import itertools
import json
import os
import time
import traceback
from typing import Coroutine, Iterable, List, Optional, Callable

from dask.distributed import Client as DaskClient
from dask.distributed import LocalCluster as DaskLocalCluster
from dask.distributed import wait as dask_wait
from distributed.scheduler import KilledWorker
from distributed.utils import CancelledError
from dask_cloudprovider.aws import FargateCluster as DaskFargateCluster
import dotenv
import httpx
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

    res = await amap(coroutine, data, num_workers)
    return res

def thread_map(function, args, num_workers=10):
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        return list(executor.map(function, args))

def wait_until(condition, interval=0.1, timeout=1, *args):
    start = time.time()
    while not condition(*args) and time.time() - start < timeout:
        time.sleep(interval)
    if time.time() - start >= timeout:
        raise TimeoutError("Timed out waiting for condition")

class DaskCluster:
    def __init__(self, cluster_type, worker_nthreads=1, worker_cpu=256, worker_mem=512):
        if cluster_type == 'fargate':
            self.cluster = DaskFargateCluster(
                fargate_spot=True,
                image="daskdev/dask:latest-py3.10", 
                environment={'EXTRA_PIP_PACKAGES': 'httpx==0.27.0 tqdm==4.66.2 lz4==4.3.3 msgpack==1.0.8 toolz==0.12.1'},
                worker_cpu=worker_cpu,
                worker_nthreads=worker_nthreads,
                worker_mem=worker_mem,
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

def dask_map(function, args, num_workers=16, reqs_per_ip=1000, batch_size=100000, task_batch_size=1000, max_task_tries=3, task_nthreads=1, task_timeout=10, worker_cpu=256, worker_mem=512, cluster_type='local'):
    function = DaskBatchFunc(DaskFunc(function), task_nthreads=task_nthreads)
    tasks = [DaskTask(arg) for arg in args]
    dotenv.load_dotenv()
    tasks_progress_bar = tqdm(total=len(tasks), desc="All Tasks")
    batch_progress_bar = tqdm(total=min(batch_size, len(tasks)), desc="Batch Tasks", leave=False)
    while len([t for t in tasks if not t.completed]) > 0:
        try:
            with DaskCluster(cluster_type, worker_cpu=worker_cpu, worker_mem=worker_mem) as cluster:
                with DaskClient(cluster) as client:
                    if hasattr(cluster, 'adapt'):
                        cluster.adapt(minimum=1, maximum=num_workers)
                        # wait for workers to start
                        client.wait_for_workers(1, timeout=120)
                    num_reqs_for_current_ips = 0
                    while len([t for t in tasks if not t.completed]) > 0:
                        try:
                            current_batch_size = min(batch_size, len([t for t in tasks if not t.completed])) 
                            # batching tasks as we want to avoid having dask tasks that are too small
                            batch_tasks = [t for t in tasks if not t.completed][:current_batch_size]
                            all_batch_args = [t.args for t in batch_tasks]
                            num_reqs_for_current_ips += current_batch_size
                            batch_args = []
                            for i in range(0, len(all_batch_args), task_batch_size):
                                batch_args.append(all_batch_args[i:i+task_batch_size])
                            batch_progress_bar.reset(total=current_batch_size)
                            task_futures = client.map(function, batch_args)
                            for f in task_futures:
                                # TODO update with specific task arg size, not larger batch size
                                f.add_done_callback(lambda _: batch_progress_bar.update(task_batch_size))
                            # wait for the futures to complete, with a timeout
                            try:
                                total_time = len(batch_tasks) * task_timeout
                                timeout = total_time / (num_workers * task_nthreads)
                                dask_wait(task_futures, timeout=timeout, return_when="ALL_COMPLETED")
                            except Exception:
                                for f in task_futures:
                                    if f.status != "finished":
                                        f.cancel()
                            # get all the results
                            batch_result = []
                            for f, args in zip(task_futures, batch_args):
                                try:
                                    batch_result.append(f.result())
                                except CancelledError as e:
                                    timeout_ex = TimeoutError(f"Task timed out after {timeout} seconds")
                                    batch_result.append([{'res': None, 'exception': timeout_ex, 'pre_time': None, 'post_time': datetime.datetime.now()} for _ in args])
                                except KilledWorker as e:
                                    batch_result.append([{'res': None, 'exception': e, 'pre_time': None, 'post_time': datetime.datetime.now()} for _ in args])
                            
                            batch_result = [r for batch in batch_result for r in batch]
                            # sort out task returns, either they complete, 
                            # or had exceptions (which we want to keep track of), and need to be tried, or we give up
                            for t, r in zip(batch_tasks, batch_result):
                                if r['exception'] is not None:
                                    t.exceptions.append(r)
                                    if len(t.exceptions) >= max_task_tries:
                                        t.completed = True
                                        tasks_progress_bar.update(1)
                                else:
                                    t.result = r
                                    t.completed = True
                                    tasks_progress_bar.update(1)
                            if hasattr(cluster, 'scale') and num_reqs_for_current_ips >= reqs_per_ip * num_workers:
                                # recreate workers to get new IPs
                                cluster.scale(0)
                                wait_until(lambda: len(client.scheduler_info()['workers']) == 0, timeout=120)
                                num_reqs_for_current_ips = 0
                                cluster.adapt(minimum=1, maximum=num_workers)
                                client.wait_for_workers(1, timeout=120)
                        # catch exceptions that are recoverable without restarting the cluster
                        except Exception as e:
                            if client.scheduler_info(): # client is still connected
                                print(f"Batch Error: {e}, Stacktrace: {traceback.format_exc()}")
                                continue
                            else:
                                raise
                                
        except Exception as ex:
            print(f"Cluster Restart Error: {ex}, Stacktrace: {traceback.format_exc()}")
    tasks_progress_bar.close()
    batch_progress_bar.close()

    return tasks

class InvalidResponseException(Exception):
    pass

class NotFoundException(Exception):
    pass


def get_headers():
    headers = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Accept-Language': 'en-CA',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
    }
    return headers

def process_response(r):
    if r.status_code != 200:
        raise InvalidResponseException(
            r.text, f"TikTok returned a {r.status_code} status code."
        )

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
    url = f"https://www.tiktok.com/@/video/{video_id}"
    headers = get_headers()
    
    try:
        with httpx.Client() as client:
            with client.stream("GET", url, headers=headers) as r:
                if r.status_code != 200:
                    raise InvalidResponseException(
                        r.text, f"TikTok returned a {r.status_code} status code."
                    )
                text = ""
                start = -1
                json_start = '"webapp.video-detail":'
                json_start_len = len(json_start)
                end = -1
                json_end = ',"webapp.a-b":'

                for text_chunk in r.iter_text():
                    text += text_chunk
                    if len(text) < json_start_len:
                        continue
                    if start == -1:
                        start = text.find(json_start)
                        if start != -1:
                            start = 0
                            text = text[start + json_start_len:]
                    if start != -1:
                        end = text.find(json_end)
                        if end != -1:
                            text = text[:end]
                            break

                if start == -1 or end == -1:
                    raise InvalidResponseException(
                        text, "Could not find normal JSON section in returned HTML."
                    )
                video_detail = json.loads(text)
                if video_detail.get("statusCode", 0) != 0: # assume 0 if not present
                    return video_detail
                video_info = video_detail.get("itemInfo", {}).get("itemStruct")
                if video_info is None:
                    raise InvalidResponseException(
                        r.text, "TikTok JSON did not contain expected JSON."
                    )
                return video_info
    except Exception as ex:
        raise InvalidResponseException(
            "TikTok returned an invalid response."
        )

class DaskFunc:
    def __init__(self, func):
        self.func = func

    def __call__(self, arg):
        
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
    
class DaskBatchFunc:
    def __init__(self, func, task_nthreads=1):
        self.func = func
        self.task_nthreads = task_nthreads

    def __call__(self, batch_args):
        return thread_map(self.func, batch_args, num_workers=self.task_nthreads)
    
class AsyncDaskFunc:
    def __init__(self, func):
        self.func = func

    async def __call__(self, arg):
        
        pre_time = datetime.datetime.now()
        try:
            res = await self.func(arg)
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
    
class AsyncDaskBatchFunc:
    def __init__(self, func, task_nthreads=1):
        self.func = func
        self.task_nthreads = task_nthreads

    async def __call__(self, batch_args):
        return async_map(self.func, batch_args, num_workers=self.task_nthreads)

class DaskTask:
    def __init__(self, args):
        self.args = args
        self.exceptions = []
        self.result = None
        self.completed = False
    

def main():
    this_dir_path = os.path.dirname(os.path.realpath(__file__))
    
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
    num_time = 1
    time_unit = 's'
    unit_map = {
        'ms': 'milliseconds',
        's': 'seconds',
        'm': 'minutes',
    }
    time_delta = datetime.timedelta(**{unit_map[time_unit]: num_time})
    start_time = datetime.datetime(2024, 3, 1, 20, 0, 1)
    end_time = start_time + time_delta
    c_time = start_time
    all_timestamp_bits = []
    while c_time < end_time:
        unix_timestamp_bits = format(int(c_time.timestamp()), '032b')
        milliseconds = int(format(c_time.timestamp(), '.3f').split('.')[1])
        milliseconds_bits = format(milliseconds, '010b')
        timestamp_bits = unix_timestamp_bits + milliseconds_bits
        all_timestamp_bits.append(timestamp_bits)
        c_time += datetime.timedelta(milliseconds=1)

    potential_video_bits = itertools.product(all_timestamp_bits, other_bit_sequences)
    potential_video_bits = [''.join(bits) for bits in potential_video_bits]
    potential_video_ids = [int(bits, 2) for bits in potential_video_bits]
    num_workers = 64
    reqs_per_ip = 5000
    batch_size = 100000
    task_batch_size = 200
    task_nthreads = 12
    task_timeout = 10
    worker_cpu = 256
    worker_mem = 512
    cluster_type = 'fargate'
    # r = await async_map(test_real_video, potential_video_ids, num_workers=64)
    results = dask_map(
        get_video, 
        potential_video_ids, 
        num_workers=num_workers, 
        reqs_per_ip=reqs_per_ip, 
        batch_size=batch_size,
        task_batch_size=task_batch_size,
        task_timeout=task_timeout,
        task_nthreads=task_nthreads, 
        worker_cpu=worker_cpu, 
        worker_mem=worker_mem,
        cluster_type=cluster_type
    )
    num_hits = len([r for r in results if r.result and 'id' in r.result['res']])
    num_valid = len([r for r in results if r.completed])
    print(f"Num hits: {num_hits}, Num valid: {num_valid}, Num potential video IDs: {len(potential_video_ids)}")
    print(f"Fraction hits: {num_hits / num_valid}")
    print(f"Fraction valid: {num_valid / len(potential_video_ids)}")
    # convert to jsonable format
    json_results = [
        {
            'args': r.args, 
            'exceptions': [{
                    'exception': str(e['exception']),
                    'pre_time': e['pre_time'].isoformat() if e['pre_time'] else None,
                    'post_time': e['post_time'].isoformat()
                }
                for e in r.exceptions
            ], 
            'result': {
                'return': r.result['res'] if r.result is not None else None,
                'pre_time': r.result['pre_time'].isoformat() if r.result is not None else None,
                'post_time': r.result['post_time'].isoformat() if r.result is not None else None
            },
            'completed': r.completed
        }
        for r in results
    ]

    results_dir_path = os.path.join(this_dir_path, '..', 'data', 'results')
    results_dirs = [dir_name for dir_name in os.listdir(results_dir_path)]
    new_result_dir = str(max([int(d) for d in results_dirs]) + 1) if results_dirs else '0'
    os.mkdir(os.path.join(results_dir_path, new_result_dir))

    params = {
        'start_time': start_time.isoformat(),
        'end_time': end_time.isoformat(),
        'num_time': num_time,
        'time_unit': time_unit,
        'num_workers': num_workers,
        'reqs_per_ip': reqs_per_ip,
        'batch_size': batch_size,
        'task_batch_size': task_batch_size,
        'task_timeout': task_timeout,
        'task_nthreads': task_nthreads,
        'worker_cpu': worker_cpu,
        'worker_mem': worker_mem,
        'cluster_type': cluster_type,
    }

    with open(os.path.join(this_dir_path, '..', 'data', 'results', new_result_dir, 'parameters.json'), 'w') as f:
        json.dump(params, f)

    with open(os.path.join(this_dir_path, '..', 'data', 'results', new_result_dir, 'results.json'), 'w') as f:
        json.dump(json_results, f)

if __name__ == '__main__':
    main()