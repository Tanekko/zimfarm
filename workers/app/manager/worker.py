#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# vim: ai ts=4 sts=4 et sw=4 nu

import os
import json
import time
import signal
import datetime

import zmq
import requests
import humanfriendly

from common import logger
from common.worker import BaseWorker
from common.docker import (
    query_host_stats,
    stop_task_worker,
    start_task_worker,
    get_label_value,
    list_containers,
    remove_container,
)
from common.constants import CANCELED, CANCEL_REQUESTED, SUPPORTED_OFFLINERS


class WorkerManager(BaseWorker):
    poll_interval = os.getenv("POLL_INTERVAL", 600)  # seconds between each manual poll
    sleep_interval = os.getenv("SLEEP_INTERVAL", 5)  # seconds to sleep while idle
    events = ["requested-task", "requested-tasks", "cancel-task"]
    config_keys = ["poll_interval", "sleep_interval", "events"]

    def __init__(self, **kwargs):
        # include our class config values in the config print
        kwargs.update({k: getattr(self, k) for k in self.config_keys})
        kwargs.update({"OFFLINERS": SUPPORTED_OFFLINERS})
        self.print_config(**kwargs)

        # set data holders
        self.tasks = {}
        self.last_poll = None
        self.should_stop = False

        # check workdir
        self.check_workdir()

        # check SSH private key
        self.check_private_key()

        # ensure we have valid credentials
        self.check_auth()

        # ensure we have access to docker API
        self.check_docker()

        # display resources
        host_stats = query_host_stats(self.docker, self.workdir)
        logger.info(
            "Host hardware resources:"
            "\n\tCPU : {cpu_total} (total) ;  {cpu_avail} (avail)"
            "\n\tRAM : {mem_total} (total) ;  {mem_avail} (avail)"
            "\n\tDisk: {disk_avail} (avail)".format(
                mem_total=humanfriendly.format_size(
                    host_stats["memory"]["total"], binary=True
                ),
                mem_avail=humanfriendly.format_size(
                    host_stats["memory"]["available"], binary=True
                ),
                cpu_total=host_stats["cpu"]["total"],
                cpu_avail=host_stats["cpu"]["available"],
                disk_avail=humanfriendly.format_size(
                    host_stats["disk"]["available"], binary=True
                ),
            )
        )

        self.check_in()

        # register stop/^C
        self.register_signals()

        self.sync_tasks_and_containers()
        self.poll()

    @property
    def should_poll(self):
        return (
            datetime.datetime.now() - self.last_poll
        ).total_seconds() > self.poll_interval

    def sleep(self):
        time.sleep(self.sleep_interval)

    def poll(self, task_id=None):
        logger.debug("polling…")
        self.last_poll = datetime.datetime.now()

        host_stats = query_host_stats(self.docker, self.workdir)
        success, status_code, response = self.query_api(
            "GET",
            "/requested-tasks/",
            params={
                "limit": 1,
                "worker": self.worker_name,
                "matching_cpu": host_stats["cpu"]["available"],
                "matching_memory": host_stats["memory"]["available"],
                "matching_disk": host_stats["disk"]["available"],
                "matching_offliners": SUPPORTED_OFFLINERS,
            },
        )
        if success and response["items"]:
            logger.info(
                "API is offering {nb} task(s): {ids}".format(
                    nb=len(response["items"]),
                    ids=[task["_id"] for task in response["items"]],
                )
            )
            self.start_task(response["items"].pop())
            self.poll()

    def check_in(self):
        """ inform backend that we started a manager, sending resources info """
        logger.info(f"checking-in with the API…")

        host_stats = query_host_stats(self.docker, self.workdir)
        success, status_code, response = self.query_api(
            "PUT",
            f"/workers/{self.worker_name}/check-in",
            payload={
                "username": self.username,
                "cpu": host_stats["cpu"]["total"],
                "memory": host_stats["memory"]["total"],
                "disk": host_stats["disk"]["total"],
                "offliners": SUPPORTED_OFFLINERS,
            },
        )
        if not success:
            logger.error("\tunable to check-in with the API.")
            logger.debug(status_code)
            logger.debug(response)
            raise SystemExit()
        logger.info("\tchecked-in!")

    def check_cancellation(self):
        for task_id, task in self.tasks.items():
            if task["status"] in [CANCELED, CANCEL_REQUESTED]:
                continue  # already handling cancellation

            self.update_task_data(task_id)
            if task["status"] in [CANCELED, CANCEL_REQUESTED]:
                self.cancel_and_remove_task(task_id)

    def cancel_and_remove_task(self, task_id):
        self.stop_task_worker(task_id)
        self.tasks.pop(task_id, None)

    def update_task_data(self, task_id):
        """ request task object from server and update locally """

        logger.debug(f"update_task_data: {task_id}")
        success, status_code, response = self.query_api("GET", f"/tasks/{task_id}")
        if success and status_code == requests.codes.OK:
            self.tasks[task_id] = response
            return True

        if status_code == requests.codes.NOT_FOUND:
            logger.warning(f"task {task_id} is gone. cancelling it")
            self.cancel_and_remove_task(task_id)
        else:
            logger.warning(f"couldn't retrieve task detail for {task_id}")
        return success

    def sync_tasks_and_containers(self):
        # list of completed containers (successfuly ran)
        completed_containers = list_containers(
            self.docker, all=True, filters={"label": ["zimtask=yes"], "exited": 0}
        )

        # list of running containers
        running_containers = list_containers(
            self.docker, filters={"label": ["zimtask=yes"]}
        )

        # list of task_ids for running containers
        running_task_ids = [
            get_label_value(self.docker, container.name, "task_id")
            for container in running_containers
        ]

        # remove completed containers
        for container in completed_containers:
            logger.info(f"container {container.name} exited successfuly, removing.")
            remove_container(self.docker, container.name)

        # make sure we are tracking task for all running containers
        for task_id in running_task_ids:
            if task_id not in self.tasks.keys():
                logger.info("found running container for {task_id}.")
                self.update_task_data(task_id)

        # filter our tasks register of gone containers
        for task_id in list(self.tasks.keys()):
            if task_id not in running_task_ids:
                logger.info("task {task_id} is not running anymore, unwatching.")
                self.tasks.pop(task_id, None)

    def stop_task_worker(self, task_id):
        logger.debug(f"stop_task_worker: {task_id}")
        stop_task_worker(self.docker, task_id, timeout=20)

    def start_task(self, requested_task):
        task_id = requested_task["_id"]
        logger.debug(f"start_task: {task_id}")

        success, status_code, response = self.query_api(
            "POST", f"/tasks/{task_id}", params={"worker_name": self.worker_name}
        )
        if success and status_code == requests.codes.CREATED:
            self.update_task_data(task_id)
            self.start_task_worker(requested_task)
        elif status_code == requests.codes.LOCKED:
            logger.warning(f"task {task_id} belongs to another worker. skipping")
        else:
            logger.warning(f"couldn't request task: {task_id}")
            logger.warning(f"HTTP {status_code}: {response}")

    def start_task_worker(self, requested_task):
        logger.debug(f"start_task_worker: {requested_task['_id']}")
        start_task_worker(
            self.docker,
            requested_task,
            self.webapi_uri,
            self.username,
            self.workdir,
            self.worker_name,
        )

    def handle_broadcast_event(self, received_string):
        try:
            key, data = received_string.split(" ", 1)
            payload = json.loads(data)
            logger.info(f"received {key} - {data[:100]}")
            logger.debug(f"received: {key} – {json.dumps(payload, indent=4)}")
        except Exception as exc:
            logger.exception(exc)
            logger.info(received_string)

        if key == "cancel-task":
            self.cancel_and_remove_task(data)
        elif key in ("requested-task", "requested-tasks"):
            # incoming task. wait <nb-running> x <sleep_itvl> seconds before polling
            # to allow idle workers to pick this up first
            time.sleep(self.sleep_interval * len(self.tasks))
            self.poll()

    def exit_gracefully(self, signum, frame):
        signame = signal.strsignal(signum)
        logger.info(f"received exit signal ({signame}), stopping worker…")
        self.should_stop = True
        for task_id in self.tasks.keys():
            self.stop_task_worker(task_id)
        logger.info("clean-up successful")

    def run(self):
        context = zmq.Context()
        socket = context.socket(zmq.SUB)

        logger.info(f"subscribing to events from {self.socket_uri}…")
        socket.connect(self.socket_uri)
        for event in self.events:
            socket.setsockopt_string(zmq.SUBSCRIBE, event)

        while not self.should_stop:
            try:
                received_string = socket.recv_string(zmq.DONTWAIT)
                self.handle_broadcast_event(received_string)
            except zmq.Again:
                pass

            if self.should_poll:
                self.sync_tasks_and_containers()
                self.check_cancellation()  # update our tasks register
                self.poll()
            else:
                self.sleep()