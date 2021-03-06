#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import gzip
import sys
import glob
import logging
import collections
from optparse import OptionParser
# brew install protobuf
# protoc  --python_out=. ./appsinstalled.proto
# pip install protobuf
import appsinstalled_pb2
# pip install python-memcached
import memcache
import threading
import Queue
from functools import partial
import multiprocessing
from time import time, sleep


NORMAL_ERR_RATE = 0.01
AppsInstalled = collections.namedtuple("AppsInstalled", ["dev_type", "dev_id", "lat", "lon", "apps"])


def dot_rename(path):
    head, fn = os.path.split(path)
    # atomic in most cases
    os.rename(path, os.path.join(head, "." + fn))


def insert_appsinstalled(memc_pool, memc_addr, appsinstalled, dry_run=False):
    """Writes installed applications to memcache"""
    ua = appsinstalled_pb2.UserApps()
    ua.lat = appsinstalled.lat
    ua.lon = appsinstalled.lon
    key = "%s:%s" % (appsinstalled.dev_type, appsinstalled.dev_id)
    ua.apps.extend(appsinstalled.apps)
    packed = ua.SerializeToString()
    try:
        if dry_run:
            logging.debug("%s - %s -> %s" % (memc_addr, key, str(ua).replace("\n", " ")))
        else:
            try:
                memc = memc_pool.get(timeout=0.1)
            except Queue.Empty:
                memc = memcache.Client([memc_addr], socket_timeout=3.0)
            ok = False
            for n in range(3):
                ok = memc.set(key, packed)
                if ok:
                    break
                sleep(0.5)
            memc_pool.put(memc)
    except Exception, e:
        logging.exception("Cannot write to memc %s: %s" % (memc_addr, e))
        return False
    return True


def parse_appsinstalled(line):
    """Returns AppsInstalled object from parsed line"""
    line_parts = line.strip().split("\t")
    if len(line_parts) < 5:
        return
    dev_type, dev_id, lat, lon, raw_apps = line_parts
    if not dev_type or not dev_id:
        return
    try:
        apps = [int(a.strip()) for a in raw_apps.split(",")]
    except ValueError:
        apps = [int(a.strip()) for a in raw_apps.split(",") if a.isidigit()]
        logging.info("Not all user apps are digits: `%s`" % line)
    try:
        lat, lon = float(lat), float(lon)
    except ValueError:
        logging.info("Invalid geo coords: `%s`" % line)
    return AppsInstalled(dev_type, dev_id, lat, lon, apps)


def main(options):
    ts = time()
    num_processes = multiprocessing.cpu_count()
    pool = multiprocessing.Pool(processes=num_processes)
    fnames = sorted(fn for fn in glob.iglob(options.pattern))
    for fn in fnames:
        faddrs = list(getchunks(fn, num_processes))
        handler = partial(handle_log, fn=fn, options=options)
        for faddr in pool.imap(handler, faddrs):
            pass
        dot_rename(fn)

    logging.info('Took %s', time() - ts)


def getchunks(file_, chunks_number):
    """Break gz file into chunks and returns list of pairs of chunk start address and offset"""
    f = gzip.open(file_)
    size = os.stat(file_).st_size/chunks_number
    while 1:
        start = f.tell()
        f.seek(size, 1)
        s = f.readline()
        yield start, f.tell() - start
        if not s:
            break


def handle_log(faddrs, fn, options):
    """Handles chunk of file, counts error rate"""
    device_memc = {
        "idfa": options.idfa,
        "gaid": options.gaid,
        "adid": options.adid,
        "dvid": options.dvid,
    }

    chunk_end = int(faddrs[0]) + int(faddrs[1])
    chunk_start = int(faddrs[0])

    pools = collections.defaultdict(Queue.Queue)
    results = []
    job_queue = Queue.Queue(maxsize=100)
    processed = errors = 0

    workers = []
    for i in range(4):
        thread = threading.Thread(target=handle_thread, args=(job_queue, results))
        thread.daemon = True
        workers.append(thread)

    for thread in workers:
        thread.start()

    logging.info('Processing %s' % fn)
    with gzip.open(fn) as fd:
        fd.seek(chunk_start)
        while int(fd.tell()) < chunk_end:
            line = fd.readline()
            line = line.strip()
            if not line:
                continue
            appsinstalled = parse_appsinstalled(line)
            if not appsinstalled:
                errors += 1
                continue
            memc_addr = device_memc.get(appsinstalled.dev_type)
            if not memc_addr:
                errors += 1
                logging.error("Unknown device type: %s" % appsinstalled.dev_type)
                continue

            job_queue.put((pools[memc_addr], memc_addr, appsinstalled, options.dry))

            if not all(thread.is_alive() for thread in workers):
                break

    for thread in workers:
        if thread.is_alive():
            thread.join()

    processed, errors = [sum(x) for x in zip(*results)]

    if processed:
        err_rate = float(errors) / processed
        if err_rate < NORMAL_ERR_RATE:
            logging.info("Acceptable error rate (%s). Successfull load" % err_rate)
        else:
            logging.error("High error rate (%s > %s). Failed load" % (err_rate, NORMAL_ERR_RATE))


def handle_thread(job_queue, results):
    """Handles thread task to put installed applications into memcache"""
    processed = errors = 0
    while True:
        try:
            task = job_queue.get(timeout=0.1)
        except Queue.Empty:
            results.append((processed, errors))
            return

        memc_pool, memc_addr, appsinstalled, dry_run = task
        ok = insert_appsinstalled(memc_pool, memc_addr, appsinstalled, dry_run)

        if ok:
            processed += 1
        else:
            errors += 1


def prototest():
    sample = "idfa\t1rfw452y52g2gq4g\t55.55\t42.42\t1423,43,567,3,7,23\ngaid\t7rfw452y52g2gq4g\t55.55\t42.42\t7423,424"
    for line in sample.splitlines():
        dev_type, dev_id, lat, lon, raw_apps = line.strip().split("\t")
        apps = [int(a) for a in raw_apps.split(",") if a.isdigit()]
        lat, lon = float(lat), float(lon)
        ua = appsinstalled_pb2.UserApps()
        ua.lat = lat
        ua.lon = lon
        ua.apps.extend(apps)
        packed = ua.SerializeToString()
        unpacked = appsinstalled_pb2.UserApps()
        unpacked.ParseFromString(packed)
        assert ua == unpacked


if __name__ == '__main__':
    op = OptionParser()
    op.add_option("-t", "--test", action="store_true", default=False)
    op.add_option("-l", "--log", action="store", default=None)
    op.add_option("--dry", action="store_true", default=False)
    op.add_option("--pattern", action="store", default="/data/appsinstalled/*.tsv.gz")
    op.add_option("--idfa", action="store", default="127.0.0.1:33013")
    op.add_option("--gaid", action="store", default="127.0.0.1:33014")
    op.add_option("--adid", action="store", default="127.0.0.1:33015")
    op.add_option("--dvid", action="store", default="127.0.0.1:33016")
    (opts, args) = op.parse_args()
    logging.basicConfig(filename=opts.log, level=logging.INFO if not opts.dry else logging.DEBUG,
                        format='[%(asctime)s] %(levelname).1s %(message)s', datefmt='%Y.%m.%d %H:%M:%S')
    if opts.test:
        prototest()
        sys.exit(0)

    logging.info("Memc loader started with options: %s" % opts)
    try:
        main(opts)
    except Exception, e:
        logging.exception("Unexpected error: %s" % e)
        sys.exit(1)
