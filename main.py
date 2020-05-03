import base64
import json
import logging
import multiprocessing
import os
from asyncio import Lock, get_event_loop
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import List
from contextlib import contextmanager
from weakref import WeakValueDictionary

import lmdb
from aiohttp import web
from aiohttp.web import Request
from rchain.client import RClient
from rchain.param import mainnet_param
from rchain.report import DeployWithTransaction

DB_PATH = os.environ.get("DB_PATH", "transactionsDB")
TARGET_RNODE_HOST = os.environ.get("TARGET_RNODE_HOST")
TARGET_RNODE_PORT = os.environ.get('TARGET_RNODE_PORT', 40401)
HOST = os.environ.get('HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', 7070))
NUM_CORE = os.environ.get("NUM_CORE", multiprocessing.cpu_count() * 2)
LOG_PATH = os.environ.get("LOG_PATH", "/var/log/transactions.log")
MAX_MEM = int(os.environ.get("MAX_MEM", 10)) * 1048576  # Megabytes

handler = logging.FileHandler(LOG_PATH)
handler.setLevel(logging.INFO)
root = logging.getLogger()
root.addHandler(handler)
root.setLevel(logging.INFO)

# LockControll = defaultdict(Lock)
LockControll = WeakValueDictionary()

executor = ThreadPoolExecutor(NUM_CORE)

# unused because of uncertainty
class LMDBWrapper:
    def __init__(self, path, map_size):
        self.path = path
        self.map_size = map_size
        self._db = lmdb.open(path, map_size=map_size)

    @contextmanager
    def begin(self, write=False):
        try:
            with self._db.begin(write=write) as txn:
                yield txn
        except (lmdb.MapResizedError, lmdb.MemoryError):
            self._db.close()
            self._db = lmdb.open(path=self.path, map_size=self.map_size)


lmdb_env = lmdb.open(DB_PATH, map_size=MAX_MEM)


def to_dict(deploy_transactions: List[DeployWithTransaction]):
    result = []
    for deploy in deploy_transactions:
        deploy_info = {
            'deployer': deploy.deploy_info.deployer,
            'term': deploy.deploy_info.term,
            'timestamp': deploy.deploy_info.timestamp,
            'sig': deploy.deploy_info.sig,
            'sigAlgorithm': deploy.deploy_info.sigAlgorithm,
            'phloPrice': deploy.deploy_info.phloPrice,
            'phloLimit': deploy.deploy_info.phloLimit,
            'validAfterBlockNumber': deploy.deploy_info.validAfterBlockNumber,
            'cost': deploy.deploy_info.cost,
            'errored': deploy.deploy_info.errored,
            'systemDeployError': deploy.deploy_info.systemDeployError
        }
        one_deploy = [{
            'fromAddr': transaction.from_addr,
            'toAddr': transaction.to_addr,
            'amount': transaction.amount,
            'retUnforeable': base64.encodebytes(transaction.ret_unforgeable.SerializeToString()).decode('utf8'),
            'deploy': deploy_info,
            'success': transaction.success[0],
            'reason': transaction.success[1]
        } for transaction in deploy.transactions]

        result.append(one_deploy)
    return result


def fetch_transactions(block_hash: str):
    client = RClient(TARGET_RNODE_HOST, TARGET_RNODE_PORT,
                     (('grpc.keepalive_time_ms', 10000), ('grpc.max_receive_message_length', 1619430400),),
                     True)
    client.install_param(mainnet_param)
    logging.info("request {} getTransaction from server {}".format(block_hash, TARGET_RNODE_HOST))
    transactions = client.get_transaction(block_hash)
    logging.info("receive {} getTransaction {} from server {}".format(block_hash, transactions, TARGET_RNODE_HOST))
    client.close()
    return json.dumps(to_dict(transactions)).encode('utf8')


async def get_transactions(block_hash: str):
    loop = get_event_loop()
    return await loop.run_in_executor(executor, fetch_transactions, block_hash)


async def handle(request: Request):
    block_hash: str = request.match_info['blockHash']
    logging.info("Receive request on blockhash {} from {}".format(block_hash, request.remote))
    lock = LockControll.get(block_hash)
    if lock is None:
        lock = Lock()
        LockControll[block_hash] = lock
    block_hash_b: bytes = block_hash.encode('utf8')
    async with lock:
        with lmdb_env.begin() as txn:
            result = txn.get(block_hash_b)
        if result is None:
            logging.info("There no result in database for {}, fetch data from server".format(block_hash))
            result = await get_transactions(block_hash)
            logging.info("Done fetch {} result {} from server".format(block_hash, result))
            with lmdb_env.begin(write=True) as w_txn:
                w_txn.put(block_hash_b, result)
            logging.info("put data {} into db".format(block_hash))
        else:
            logging.info("The data {} , {} is already in db".format(block_hash, result))
    return web.Response(body=result, headers={"Content-Type": "application/json"})


async def handle_status(request: Request):
    return web.Response(body="OK")


app = web.Application()
app.router.add_get('/getTransaction/{blockHash}', handle)
app.router.add_get('/status', handle_status)

web.run_app(app, host=HOST, port=PORT)
