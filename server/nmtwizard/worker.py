import time
import json
import logging
import six

from nmtwizard import task


class Worker(object):

    def __init__(self, redis, services, ttl_policy, refresh_counter, quarantine_time, worker_id):
        self._redis = redis
        self._services = services
        self._logger = logging.getLogger('worker')
        self._worker_id = worker_id
        self._refresh_counter = refresh_counter
        self._quarantine_time = quarantine_time
        task.set_ttl_policy(ttl_policy)

    def run(self):
        self._logger.info('Starting worker')

        # Subscribe to beat expiration.
        pubsub = self._redis.pubsub()
        pubsub.psubscribe('__keyspace@0__:beat:*')
        pubsub.psubscribe('__keyspace@0__:queue:*')
        counter = 0
        counter_beat = 1000

        while True:
            counter_beat += 1
            if counter_beat > 1000:
                counter_beat = 0
                self._redis.expire(self._worker_id, 360)

            message = pubsub.get_message()
            if message:
                channel = message['channel']
                data = message['data']
                if data == 'expired':
                    if channel.startswith('__keyspace@0__:beat:'):
                        task_id = channel[20:]
                        service = self._redis.hget('task:'+task_id, 'service')
                        if service in self._services:
                            self._logger.info('%s: task expired', task_id)
                            with self._redis.acquire_lock(task_id):
                                task.terminate(self._redis, task_id, phase='expired')
                    elif channel.startswith('__keyspace@0__:queue:'):
                        task_id = channel[21:]
                        service = self._redis.hget('task:'+task_id, 'service')
                        if service in self._services:
                            self._logger.info('%s: move to work queue', task_id)
                            task.work_queue(self._redis, task_id, service)
            else:
                for service in self._services:
                    task_id = task.work_unqueue(self._redis, service)
                    if task_id is not None:
                        try:
                            self._advance_task(task_id)
                        except RuntimeWarning:
                            self._logger.warning(
                                '%s: failed to acquire a lock, retrying', task_id)
                            task.work_queue(self._redis, task_id, service)
                        except Exception as e:
                            self._logger.error('%s: %s', task_id, str(e))
                            with self._redis.acquire_lock(task_id):
                                task.set_log(self._redis, task_id, str(e))
                                task.terminate(self._redis, task_id, phase="launch_error")
                    else:
                        if counter > self._refresh_counter:
                            resources = self._services[service].list_resources()
                            for resource in resources:                                    
                                keyr = 'gpu_resource:%s:%s' % (service, resource)
                                key_busy = 'busy:%s:%s' % (service, resource)
                                key_reserved = 'reserved:%s:%s' % (service, resource)
                                if not self._redis.exists(key_busy) and self._redis.hlen(keyr) < resources[resource]:
                                    if self._redis.exists(key_reserved) and self._redis.ttl('queue:'+self._redis.get(key_reserved))>10:
                                        self._redis.expire('queue:'+self._redis.get(key_reserved), 5)
                                        break
                            if self._redis.exists('queued:%s' % service):
                                resources = self._services[service].list_resources()
                                self._logger.debug('checking processes on : %s', service)
                                availableResource = False
                                for resource in resources:                                    
                                    keyr = 'gpu_resource:%s:%s' % (service, resource)
                                    key_busy = 'busy:%s:%s' % (service, resource)
                                    key_reserved = 'reserved:%s:%s' % (service, resource)
                                    # try dequeuing only if resource not busy, not reserved, and is pure cpu
                                    # or has cpu available
                                    if not self._redis.exists(key_busy) and (
                                            resources[resource]==0 or self._redis.hlen(keyr) < resources[resource]):
                                        if not self._redis.exists(key_reserved):
                                            availableResource = True
                                        break
                                if availableResource:
                                    self._logger.debug('resources available on %s - trying dequeuing', service)
                                    self._service_unqueue(self._services[service])
                if counter > self._refresh_counter:
                    counter = 0

            counter += 1
            time.sleep(0.01)

    def _advance_task(self, task_id):
        """Tries to advance the task to the next status. If it can, re-queue it immediately
        to process the next stage. Otherwise, re-queue it after some delay to try again.
        """
        keyt = 'task:%s' % task_id
        with self._redis.acquire_lock(keyt, acquire_timeout=1, expire_time=600):
            status = self._redis.hget(keyt, 'status')
            if status == 'stopped':
                return

            service_name = self._redis.hget(keyt, 'service')
            if service_name not in self._services:
                raise ValueError('unknown service %s' % service_name)
            service = self._services[service_name]

            self._logger.info('%s: trying to advance from status %s', task_id, status)

            if status == 'queued':
                resource = self._redis.hget(keyt, 'resource')
                parent = self._redis.hget(keyt, 'parent')
                if parent:
                    keyp = 'task:%s' % parent
                    # if the parent task is in the database, check for dependencies
                    if self._redis.exists(keyp):
                        status = self._redis.hget(keyp, 'status')
                        if status == 'stopped':
                            if self._redis.hget(keyp, 'message') != 'completed':
                                task.terminate(self._redis, task_id, phase='dependency_error')
                                return
                        else:
                            self._logger.warning('%s: depending on other task, waiting', task_id)
                            task.service_queue(self._redis, task_id, service.name)
                            return
                ngpus = int(self._redis.hget(keyt, 'ngpus'))
                ncpus = int(self._redis.hget(keyt, 'ncpus'))
                resource, available_gpus = self._allocate_resource(task_id, resource, service, ngpus, ncpus)
                if resource is not None:
                    self._logger.info('%s: resource %s reserved (%d/%d)',
                                      task_id, resource, available_gpus, ngpus)
                    self._redis.hset(keyt, 'alloc_resource', resource)
                    if ngpus == available_gpus:
                        task.set_status(self._redis, keyt, 'allocated')
                    else:
                        task.set_status(self._redis, keyt, 'allocating')
                    task.work_queue(self._redis, task_id, service_name)
                else:
                    self._logger.warning('%s: no resources available, waiting', task_id)
                    task.service_queue(self._redis, task_id, service.name)
            elif status == 'allocating':
                resource = self._redis.hget(keyt, 'alloc_resource')
                keyr = 'gpu_resource:%s:%s' % (service.name, resource)
                ngpus = int(self._redis.hget(keyt, 'ngpus'))
                ncpus = int(self._redis.hget(keyt, 'ncpus'))
                already_allocated_gpus = 0
                for k, v in six.iteritems(self._redis.hgetall(keyr)):
                    if v == task_id:
                        already_allocated_gpus += 1
                capacity = service.list_resources()[resource]
                available_gpus, remaining_gpus = self._reserve_resource(service, resource,
                                                                        capacity, task_id,
                                                                        ngpus - already_allocated_gpus, ncpus,
                                                                        0, -1, True)
                self._logger.warning('task: %s - resource: %s (capacity %d)- already %d - available %d', task_id, resource, capacity, already_allocated_gpus, available_gpus)
                if available_gpus == ngpus - already_allocated_gpus:
                    task.set_status(self._redis, keyt, 'allocated')
                    key_reserved = 'reserved:%s:%s' % (service.name, resource)
                    self._redis.delete(key_reserved)
                    task.work_queue(self._redis, task_id, service.name)
                else:
                    task.work_queue(self._redis, task_id, service.name,
                                    delay=service.is_notifying_activity and 120 or 30)
            elif status == 'allocated':
                content = json.loads(self._redis.hget(keyt, 'content'))
                resource = self._redis.hget(keyt, 'alloc_resource')
                self._logger.info('%s: launching on %s', task_id, service.name)
                try:
                    keyr = 'gpu_resource:%s:%s' % (service.name, resource)
                    lgpu = []
                    for k, v in six.iteritems(self._redis.hgetall(keyr)):
                        if v == task_id:
                            lgpu.append(k)
                    if 'ncpus' in content:
                        ncpus = content['ncpus']
                    else:
                        ncpus = 2
                    self._redis.hset(keyt, 'alloc_lgpu', ",".join(lgpu))
                    data = service.launch(
                        task_id,
                        content['options'],
                        lgpu,
                        resource,
                        content['docker']['registry'],
                        content['docker']['image'],
                        content['docker']['tag'],
                        content['docker']['command'],
                        task.file_list(self._redis, task_id),
                        content['wait_after_launch'])
                except EnvironmentError as e:
                    # the resource is not available and will be set busy
                    self._block_resource(resource, service, str(e))
                    self._redis.hdel(keyt, 'alloc_resource')
                    # set the task as queued again
                    self._release_resource(service, resource, task_id, ncpus)
                    task.set_status(self._redis, keyt, 'queued')
                    task.service_queue(self._redis, task_id, service.name)
                    self._logger.info('could not launch [%s] %s on %s: blocking resource', str(e), task_id, resource)
                    return
                except Exception as e:
                    # all other errors make the task fail
                    self._logger.info('fail task [%s] - %s', task_id, str(e))
                    task.append_log(self._redis, task_id, str(e))
                    task.terminate(self._redis, task_id, phase='launch_error')
                    return
                self._logger.info('%s: task started on %s', task_id, service.name)
                self._redis.hset(keyt, 'job', json.dumps(data))
                task.set_status(self._redis, keyt, 'running')
                # For services that do not notify their activity, we should
                # poll the task status more regularly.
                task.work_queue(self._redis, task_id, service.name,
                                delay=service.is_notifying_activity and 120 or 30)

            elif status == 'running':
                self._logger.debug('- checking activity of task: %s', task_id)
                data = json.loads(self._redis.hget(keyt, 'job'))
                status = service.status(task_id, data)
                if status == 'dead':
                    self._logger.info('%s: task no longer running on %s, request termination',
                                      task_id, service.name)
                    task.terminate(self._redis, task_id, phase='exited')
                else:
                    task.work_queue(self._redis, task_id, service.name,
                                    delay=service.is_notifying_activity and 120 or 30)

            elif status == 'terminating':
                data = self._redis.hget(keyt, 'job')
                ncpus = int(self._redis.hget(keyt, 'ncpus'))
                if data is not None:
                    container_id = self._redis.hget(keyt, 'container_id')
                    data = json.loads(data)
                    data['container_id'] = container_id
                    self._logger.info('%s: terminating task (%s)', task_id, json.dumps(data))
                    try:
                        service.terminate(data)
                        self._logger.info('%s: terminated', task_id)
                    except Exception:
                        self._logger.warning('%s: failed to terminate', task_id)
                resource = self._redis.hget(keyt, 'alloc_resource')
                self._release_resource(service, resource, task_id, ncpus)
                task.set_status(self._redis, keyt, 'stopped')
                task.disable(self._redis, task_id)

    def _block_resource(self, resource, service, err):
        """Block a resource on which we could not launch a task
        """
        keyb = 'busy:%s:%s' % (service.name, resource)
        self._redis.set(keyb, err)
        self._redis.expire(keyb, self._quarantine_time)

    def _allocate_resource(self, task_id, resource, service, ngpus, ncpus):
        """Allocates a resource for task_id and returns the name of the resource
        (or None if none where allocated).
        """
        best_resource = None
        br_remaining = -1
        br_available_gpus = 0
        resources = service.list_resources()
        if resource == 'auto':
            for name, capacity in six.iteritems(resources):
                available_gpus, remaining_gpus = self._reserve_resource(service, name, capacity, task_id, ngpus,
                                                        ncpus, br_available_gpus, br_remaining)
                if available_gpus is not False:
                    if best_resource is not None:
                        self._release_resource(service, best_resource, task_id, ncpus)
                    best_resource = name
                    br_remaining = remaining_gpus
                    br_available_gpus = available_gpus
                    if available_gpus == 0:
                        break
            return best_resource, br_available_gpus
        elif resource not in resources:
            raise ValueError('resource %s does not exist for service %s' % (resource, service.name))
        else:
            available_gpus, remaining_gpus = self._reserve_resource(service, resource,
                                                                    resources[resource], task_id, ngpus, ncpus,
                                                                    0, -1)
            if available_gpus:
                return resource, available_gpus
        return None, None

    def _reserve_resource(self, service, resource, capacity, task_id, ngpus, ncpus,
                          br_available_gpus, br_remaining, check_reserved = False):
        """Reserves the resource for task_id, if possible. The resource is locked
        while we try to reserve it.
        Resource should have more gpus available (within ngpus) than br_available_gpus
        or the same number but a smaller size
        """
        if capacity < ngpus:
            return False, False
        keyr = 'gpu_resource:%s:%s' % (service.name, resource)
        keyc = 'ncpus:%s:%s' % (service.name, resource)
        key_busy = 'busy:%s:%s' % (service.name, resource)
        key_reserved = 'reserved:%s:%s' % (service.name, resource)
        with self._redis.acquire_lock(keyr):
            if self._redis.get(key_busy) is not None:
                return False, False
            if ngpus == 0:
                available_cpus = int(self._redis.get(keyc))
                self._logger.debug('**** 0 GPU required - reserving %s', resource)
                if ncpus <= available_cpus:
                    self._redis.rpush('cpu_resource:%s:%s' % (service.name, resource), task_id)
                    self._redis.decr(keyc, ncpus)
                    return 0, 0
                else:
                    return False, False
            if not check_reserved and self._redis.get(key_reserved) is not None:
                return False, False
            current_usage = self._redis.hlen(keyr)
            avail_gpu = capacity - current_usage
            used_gpu = min(avail_gpu, ngpus)
            remaining_gpus = capacity - used_gpu
            if (used_gpu > 0 and 
               ((used_gpu > br_available_gpus) or
                (used_gpu == br_available_gpus and remaining_gpus < br_remaining))):
                idx = 1
                for i in xrange(used_gpu):
                    while self._redis.hget(keyr, str(idx)) is not None:
                        idx += 1
                        assert idx <= capacity, "invalid gpu alloc for %s" % keyr
                    self._redis.hset(keyr, str(idx), task_id)
                self._redis.decr(keyc, ncpus)
                if used_gpu < ngpus:
                    self._redis.set(key_reserved, task_id)
                return used_gpu, remaining_gpus
            else:
                return False, False

    def _release_resource(self, service, resource, task_id, ncpus = 0):
        """remove the task from resource queue
        """
        keyr = 'gpu_resource:%s:%s' % (service.name, resource)
        with self._redis.acquire_lock(keyr):
            for k, v in six.iteritems(self._redis.hgetall(keyr)):
                if v == task_id:
                    self._redis.hdel(keyr, k)
            key_reserved = 'reserved:%s:%s' % (service.name, resource)
            if self._redis.get(key_reserved) == task_id:
                self._redis.delete(key_reserved)
            self._redis.lrem('cpu_resource:%s:%s' % (service.name, resource), task_id)
            if ncpus != 0:
                self._redis.incr('ncpus:%s:%s' % (service.name, resource), ncpus)

    def _service_unqueue(self, service):
        """find the best next task to push to the work queue
        """
        with self._redis.acquire_lock('service:'+service.name):
            queue = 'queued:%s' % service.name
            count = self._redis.llen(queue)
            idx = 0
            # Pop a task waiting for a resource on this service, check if it can run (dependency)
            # and queue it for a retry.
            best_task_id = None
            best_task_priority = -10000
            best_task_queued_time = 0
            while count > 0:
                count -= 1
                next_task_id = self._redis.lindex(queue, count)
                if next_task_id is not None:
                    next_keyt = 'task:%s' % next_task_id
                    parent = self._redis.hget(next_keyt, 'parent')
                    priority = int(self._redis.hget(next_keyt, 'priority'))
                    queued_time = float(self._redis.hget(next_keyt, 'queued_time'))
                    if parent:
                        keyp = 'task:%s' % parent
                        if self._redis.exists(keyp):
                            # if the parent task is in the database, check for dependencies
                            parent_status = self._redis.hget(keyp, 'status');
                            if parent_status != 'stopped':
                                if parent_status == 'running':
                                    # parent is still running so update queued time to be as close as
                                    # as possible to terminate time of parent task
                                    redis.hset(next_keyt, "queued_time", time.time())
                                continue
                    if priority > best_task_priority or (
                        priority == best_task_priority and best_task_queued_time > queued_time):
                        best_task_priority = priority
                        best_task_id = next_task_id
                        best_task_queued_time = queued_time

            if best_task_id:
                task.work_queue(self._redis, best_task_id, service.name)
                self._redis.lrem(queue, best_task_id)
