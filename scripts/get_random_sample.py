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
from dask.distributed import SpecCluster as DaskSpecCluster
from dask.distributed import as_completed as dask_as_completed
from distributed.scheduler import Scheduler as DaskScheduler
from distributed.deploy.ssh import Worker as DaskSSHWorker
from dask_cloudprovider.aws import FargateCluster as DaskFargateCluster
import dotenv
import httpx
from tqdm import tqdm
from tqdm.asyncio import tqdm as atqdm

from setup_pis import change_mac_addresses, get_hosts_with_retries, kill_workers

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

async def async_map(func, args, num_workers=10):
    all_pbar = tqdm(total=len(args))
    func = AsyncDaskFunc(func)
    tasks = [DaskTask(arg) for arg in args]
    while len([t for t in tasks if not t.completed]) > 0:
        batch_tasks = [t for t in tasks if not t.completed]
        batch_pbar = atqdm(total=len(args))
        def callback(*_):
            batch_pbar.update(1)
        res = await amap(func, batch_tasks, max_concurrent_tasks=num_workers, callback=callback)
        batch_pbar.close()
        for t, r in zip(batch_tasks, res):
            if r['exception'] is not None:
                t.exceptions.append(r)
                if len(t.exceptions) >= 3:
                    t.completed = True
                    all_pbar.update(1)
            else:
                t.result = r
                t.completed = True
                all_pbar.update(1)
    return tasks

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
        self.cluster_type = cluster_type
        self.worker_nthreads = worker_nthreads
        self.worker_cpu = worker_cpu
        self.worker_mem = worker_mem

    async def __aenter__(self):
        if self.cluster_type == 'fargate':
            self.cluster = DaskFargateCluster(
                fargate_spot=True,
                image="daskdev/dask:latest-py3.10", 
                environment={'EXTRA_PIP_PACKAGES': 'httpx==0.27.0 brotlipy==0.7.0 tqdm==4.66.2 lz4==4.3.3 msgpack==1.0.8 toolz==0.12.1'},
                worker_cpu=self.worker_cpu,
                worker_nthreads=self.worker_nthreads,
                worker_mem=self.worker_mem,
                aws_access_key_id=os.environ['AWS_ACCESS_KEY'],
                aws_secret_access_key=os.environ['AWS_SECRET_KEY'],
                cluster_arn=os.environ['ECS_CLUSTER_ARN'],
                scheduler_task_definition_arn=os.environ['SCHEDULER_TASK_DEFINITION_ARN'],
                worker_task_definition_arn=os.environ['WORKER_TASK_DEFINITION_ARN'],
                execution_role_arn=os.environ['EXECUTION_ROLE_ARN'],
                task_role_arn=os.environ['TASK_ROLE_ARN'],
                security_groups=[os.environ['SECURITY_GROUP_ID']],
                skip_cleanup=True,
                region_name='ca-central-1'
            )
        elif self.cluster_type == 'local':
            self.cluster = DaskLocalCluster()
        elif self.cluster_type == 'raspi':
            potential_usernames = [
                'hoare',
                'tarjan',
                'miacli',
                'fred',
                'geoffrey',
                'rivest',
                'edmund',
                'ivan',
                'cook',
                'barbara',
                'goldwasser',
                'milner',
                'hemming',
                'frances',
                'lee'
            ]
            hosts, usernames = await get_hosts_with_retries(potential_usernames, max_tries=10)
            connect_options = [dict(username=un, password='rp145', known_hosts=None) for un in usernames]
            connect_options = [dict(co, password='rp145', known_hosts=None) for co in connect_options]
            remote_python='~/ben/tiktok/venv/bin/python'
            worker_options={ 'nthreads': self.worker_nthreads }
            
            workers = {
                i: {
                    "cls": DaskSSHWorker,
                    "options": {
                        "address": host,
                        "connect_options": connect_options[i],
                        "kwargs": worker_options,
                        "worker_class": "distributed.Nanny",
                        "remote_python": remote_python,
                    },
                }
                for i, host in enumerate(hosts)
            }
            self.cluster = DaskSpecCluster(
                workers,
                scheduler=None,
                name="raspi-cluster",
            )
        return self.cluster
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.cluster is not None:
            self.cluster.close()

def process_future(f, batch_tasks_lookup, timeout, max_task_tries, tasks_progress_bar, exception_counter, cancel_if_unfinished=False):
    batch_tasks, processed = batch_tasks_lookup[f.key]
    if processed:
        return
    try:
        if cancel_if_unfinished and f.status != "finished":
            f.cancel()
            batch_results = [{'res': None, 'exception': TimeoutError(f"Task timed out after {timeout} seconds"), 'pre_time': None, 'post_time': datetime.datetime.now()} for _ in batch_tasks]
        else:
            batch_results = f.result()
    except Exception as e:
        batch_results = [{'res': None, 'exception': e, 'pre_time': None, 'post_time': datetime.datetime.now()} for _ in batch_tasks]
    for t, r in zip(batch_tasks, batch_results):
        if r['exception'] is not None:
            t.exceptions.append(r)
            exception_counter.add(1)
            if len(t.exceptions) >= max_task_tries:
                t.completed = True
                tasks_progress_bar.update(1)
        else:
            t.result = r
            t.completed = True
            tasks_progress_bar.update(1)
    batch_tasks_lookup[f.key] = (batch_tasks, True)

async def get_results(task_futures, batch_tasks_lookup, timeout, max_task_tries, tasks_progress_bar, exception_counter):
    for f in dask_as_completed(task_futures, raise_errors=False):
        process_future(f, batch_tasks_lookup, timeout, max_task_tries, tasks_progress_bar, exception_counter)

class Counter:
    def __init__(self):
        self.count = 0
    def add(self, n):
        self.count += n

async def dask_map(function, args, num_workers=16, reqs_per_ip=1000, batch_size=100000, task_batch_size=1000, max_task_tries=3, task_nthreads=1, task_timeout=10, worker_cpu=256, worker_mem=512, cluster_type='local'):
    function = DaskBatchFunc(DaskFunc(function), task_nthreads=task_nthreads)
    tasks = [DaskTask(arg) for arg in args]
    dotenv.load_dotenv()
    tasks_progress_bar = tqdm(total=len(tasks), desc="All Tasks")
    batch_progress_bar = tqdm(total=min(batch_size, len(tasks)), desc="Batch Tasks", leave=False)
    while len([t for t in tasks if not t.completed]) > 0:
        try:
            async with DaskCluster(cluster_type, worker_cpu=worker_cpu, worker_mem=worker_mem) as cluster:
                with DaskClient(cluster) as client:
                    if isinstance(cluster, DaskFargateCluster):
                        cluster.adapt(minimum=1, maximum=num_workers)
                        # wait for workers to start
                        client.wait_for_workers(1, timeout=120)
                    num_reqs_for_current_ips = 0
                    num_exceptions_for_current_ips = 0
                    while len([t for t in tasks if not t.completed]) > 0:
                        try:
                            current_batch_size = min(batch_size, len([t for t in tasks if not t.completed]))

                            # prepping args for mapping 
                            # batching tasks as we want to avoid having dask tasks that are too small
                            all_batch_tasks = [t for t in tasks if not t.completed][:current_batch_size]
                            batch_tasks = []
                            for i in range(0, len(all_batch_tasks), batch_size):
                                batch_tasks.append(all_batch_tasks[i:i+batch_size])
                            batch_args = [[t.args for t in batch] for batch in batch_tasks]

                            num_reqs_for_current_ips += current_batch_size

                            # reset the progress bar
                            batch_progress_bar.reset(total=current_batch_size)

                            # start the timeout timer
                            total_time = len(batch_tasks) * task_timeout
                            num_actual_workers = len(client.scheduler_info()['workers'])
                            timeout = total_time / (num_actual_workers * task_nthreads)

                            # send out the tasks
                            task_futures = client.map(function, batch_args)
                            batch_tasks_lookup = {f.key: (mini_batch_tasks, False) for mini_batch_tasks, f in zip(batch_tasks, task_futures)}
                            for f in task_futures:
                                # TODO update with specific task arg size, not larger batch size
                                f.add_done_callback(lambda _: batch_progress_bar.update(task_batch_size))

                            # wait for the futures to complete, with a timeout
                            # get all the results
                            exception_counter = Counter()
                            try:
                                await asyncio.wait_for(get_results(task_futures, batch_tasks_lookup, timeout, max_task_tries, tasks_progress_bar, exception_counter), timeout=timeout)
                            except Exception:
                                # cancel all the unfinished tasks, and add the exceptions to the task
                                for f in task_futures:
                                    process_future(f, batch_tasks_lookup, timeout, max_task_tries, tasks_progress_bar, exception_counter, cancel_if_unfinished=True)
                            num_exceptions_for_current_ips += exception_counter.count

                            # check if we need to recreate workers
                            if num_reqs_for_current_ips >= reqs_per_ip * num_actual_workers or num_exceptions_for_current_ips / num_reqs_for_current_ips >= 0.1:
                                # recreate workers to get new IPs
                                if isinstance(cluster, DaskFargateCluster):
                                    cluster.scale(0)
                                    wait_until(lambda: len(client.scheduler_info()['workers']) == 0, timeout=120)
                                    num_reqs_for_current_ips = 0
                                    cluster.adapt(minimum=1, maximum=num_workers)
                                    client.wait_for_workers(1, timeout=120)
                                elif isinstance(cluster, DaskSpecCluster):
                                    # reset mac address of raspberry pis and rescan for the new assigned IPs
                                    workers = list(cluster.workers.values())
                                    hosts = [w.address for w in workers]
                                    connect_options = [w.connect_options for w in workers]
                                    # worker processes don't seem to reliably close, so we need to kill them:(
                                    await kill_workers(hosts, connect_options) 
                                    await change_mac_addresses(hosts, connect_options)
                                    num_reqs_for_current_ips = 0
                                    num_exceptions_for_current_ips = 0
                                    raise RestartClusterException()
                        # catch exceptions that are recoverable without restarting the cluster
                        except RestartClusterException:
                            raise
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

class RestartClusterException(Exception):
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

class ProcessVideo:
    def __init__(self, r):
        self.r = r
        if r.status_code != 200:
            raise InvalidResponseException(
                r, f"TikTok returned a {r.status_code} status code."
            )
        self.text = ""
        self.start = -1
        self.json_start = '"webapp.video-detail":'
        self.json_start_len = len(self.json_start)
        self.end = -1
        self.json_end = ',"webapp.a-b":'
    
    def process_chunk(self, text_chunk):
        self.text += text_chunk
        if len(self.text) < self.json_start_len:
            return 'continue'
        if self.start == -1:
            self.start = self.text.find(self.json_start)
            if self.start != -1:
                self.text = self.text[self.start + self.json_start_len:]
                self.start = 0
        if self.start != -1:
            self.end = self.text.find(self.json_end)
            if self.end != -1:
                self.text = self.text[:self.end]
                return 'break'
        return 'continue'
            
    def process_response(self):
        if self.start == -1 or self.end == -1:
            raise InvalidResponseException(
                "Could not find normal JSON section in returned HTML.",
                json.dumps({'text': self.text, 'encoding': self.r.encoding}),
            )
        video_detail = json.loads(self.text)
        if video_detail.get("statusCode", 0) != 0: # assume 0 if not present
            return video_detail
        video_info = video_detail.get("itemInfo", {}).get("itemStruct")
        if video_info is None:
            raise InvalidResponseException(
                video_detail, "TikTok JSON did not contain expected JSON."
            )
        return video_info

async def async_get_video(video_id):
    url = f"https://www.tiktok.com/@/video/{video_id}"
    headers = get_headers()

    async with httpx.AsyncClient() as client:
        async with client.stream("GET", url, headers=headers) as r:
            video_processor = ProcessVideo(r)

            async for text_chunk in r.aiter_text():
                do = video_processor.process_chunk(text_chunk)
                if do == 'break':
                    break
                elif do == 'continue':
                    continue

            return video_processor.process_response()

def get_video(video_id):
    url = f"https://www.tiktok.com/@/video/{video_id}"
    headers = get_headers()
    
    with httpx.Client() as client:
        with client.stream("GET", url, headers=headers) as r:
            video_processor = ProcessVideo(r)

            for text_chunk in r.iter_text():
                do = video_processor.process_chunk(text_chunk)
                if do == 'break':
                    break
                elif do == 'continue':
                    continue

            return video_processor.process_response()


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
    

async def get_random_sample(
        generation_strategy,
        start_time,
        num_time,
        time_unit,
        num_workers,
        reqs_per_ip,
        batch_size,
        task_batch_size,
        task_nthreads,
        task_timeout,
        worker_cpu,
        worker_mem,
        cluster_type,
        method
    ):
    this_dir_path = os.path.dirname(os.path.realpath(__file__))
    
    with open(os.path.join(this_dir_path, '..', 'figs', 'all_videos', f'{generation_strategy}_two_segments_combinations.json'), 'r') as file:
        data = json.load(file)

    # get bits of non timestamp sections of ID
    # order dict according to interval
    data = [(tuple(map(int, interval.strip('()').split(', '))), vals) for interval, vals in data.items()]
    data = sorted(data, key=lambda x: x[0][0])
    # get rid of millisecond bits
    data = [t for t in data if t[0] != (0,9)]
    interval_bits = []
    intervals = [d[0] for d in data]
    for interval, vals in data:
        # format ints to binary
        num_bits = interval[1] - interval[0] + 1
        bits = [format(i, f'0{num_bits}b') for i in vals]
        interval_bits.append(bits)
    other_bit_sequences = itertools.product(*interval_bits)
    other_bit_sequences = [''.join(bits) for bits in other_bit_sequences]

    # get all videos in 1 millisecond
    
    unit_map = {
        'ms': 'milliseconds',
        's': 'seconds',
        'm': 'minutes',
    }
    time_delta = datetime.timedelta(**{unit_map[time_unit]: num_time})
    
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
    
    if method == 'async':
        results = await async_map(async_get_video, potential_video_ids, num_workers=num_workers)
    elif method == 'dask':
        results = await dask_map(
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
    else:
        raise ValueError("Invalid method")
    num_hits = len([r for r in results if r.result and 'id' in r.result['res']])
    num_valid = len([r for r in results if len(r.exceptions) < 3])
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

    results_dir_path = os.path.join(this_dir_path, '..', 'data', 'results', 'hours', str(start_time.hour), str(start_time.minute), str(start_time.second))
    os.makedirs(results_dir_path, exist_ok=True)
    results_dirs = [dir_name for dir_name in os.listdir(results_dir_path)]
    new_result_dir = str(max([int(d) for d in results_dirs]) + 1) if results_dirs else '0'
    os.makedirs(os.path.join(results_dir_path, new_result_dir), exist_ok=True)

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
        'generation_strategy': generation_strategy,
        'intervals': intervals,
    }

    with open(os.path.join(results_dir_path, new_result_dir, 'parameters.json'), 'w') as f:
        json.dump(params, f)

    with open(os.path.join(results_dir_path, new_result_dir, 'results.json'), 'w') as f:
        json.dump(json_results, f)

async def main():
    generation_strategy = 'all'
    # TODO run at persistent time after collection, i.e. if collection takes an hour, run after 24s after post time
    start_time = datetime.datetime(2024, 3, 1, 17, 0, 0)
    num_time = 1
    time_unit = 'ms'
    num_workers = 512
    reqs_per_ip = 2
    batch_size = 100
    task_batch_size = 10
    task_nthreads = 8
    task_timeout = 20
    worker_cpu = 256
    worker_mem = 512
    cluster_type = 'raspi'
    method = 'dask'
    if (num_time > 1 and time_unit == 's') or (time_unit == 'm') or (time_unit == 'h'):
        if time_unit == 's':
            num_seconds = num_time
        elif time_unit == 'm':
            num_seconds = num_time * 60
        elif time_unit == 'h':
            num_seconds = num_time * 3600
        else:
            raise ValueError("Invalid time unit")
        if num_seconds > 60 and cluster_type == 'fargate':
            raise ValueError("Too expensive to run for more than 60 seconds on Fargate")
        for i in range(num_seconds):
            await get_random_sample(
                generation_strategy,
                start_time + datetime.timedelta(seconds=i),
                1,
                's',
                num_workers,
                reqs_per_ip,
                batch_size,
                task_batch_size,
                task_nthreads,
                task_timeout,
                worker_cpu,
                worker_mem,
                cluster_type,
                method
            )
    else:
        await get_random_sample(
            generation_strategy,
            start_time,
            num_time,
            time_unit,
            num_workers,
            reqs_per_ip,
            batch_size,
            task_batch_size,
            task_nthreads,
            task_timeout,
            worker_cpu,
            worker_mem,
            cluster_type,
            method
        )

if __name__ == '__main__':
    asyncio.run(main())
