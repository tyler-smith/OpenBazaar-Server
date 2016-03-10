__author__ = 'chris'

import argparse
import json
import platform
import socket
import stun
import sys
import time
from api.ws import WSFactory, AuthenticatedWebSocketProtocol, AuthenticatedWebSocketFactory
from api.restapi import RestAPI
from config import DATA_FOLDER, KSIZE, ALPHA, LIBBITCOIN_SERVER,\
    LIBBITCOIN_SERVER_TESTNET, SSL_KEY, SSL_CERT, SEEDS, SSL
from daemon import Daemon
from db.datastore import Database
from dht.network import Server
from dht.node import Node
from dht.storage import PersistentStorage, ForgetfulStorage
from keys.credentials import get_credentials
from keys.keychain import KeyChain
from log import Logger, FileLogObserver
from market import network
from market.listeners import MessageListenerImpl, BroadcastListenerImpl, NotificationListenerImpl
from market.contracts import check_unfunded_for_payment
from market.profile import Profile
from net.heartbeat import HeartbeatFactory
from net.sslcontext import ChainedOpenSSLContextFactory
from net.upnp import PortMapper
from net.utils import looping_retry
from net.wireprotocol import OpenBazaarProtocol
from obelisk.client import LibbitcoinClient
from protos.objects import FULL_CONE, RESTRICTED, SYMMETRIC
from twisted.internet import reactor, task
from twisted.python import log, logfile
from txws import WebSocketFactory


def run(*args):
    TESTNET = args[0]
    LOGLEVEL = args[1]
    PORT = args[2]
    ALLOWIP = args[3]
    RESTPORT = args[4]
    WSPORT = args[5]
    HEARTBEATPORT = args[6]
    I2P = args[7]

    def start_server(keys, first_startup=False):
        # logging
        logFile = logfile.LogFile.fromFullPath(DATA_FOLDER + "debug.log", rotateLength=15000000, maxRotatedFiles=1)
        log.addObserver(FileLogObserver(logFile, level=LOGLEVEL).emit)
        log.addObserver(FileLogObserver(level=LOGLEVEL).emit)
        logger = Logger(system="OpenBazaard")

        # NAT traversal
        p = PortMapper()
        p.add_port_mapping(PORT, PORT, "UDP")
        logger.info("Finding NAT Type...")

        response = looping_retry(stun.get_ip_info, "0.0.0.0", PORT)

        logger.info("%s on %s:%s" % (response[0], response[1], response[2]))
        ip_address = response[1]
        port = response[2]

        if response[0] == "Full Cone":
            nat_type = FULL_CONE
        elif response[0] == "Restric NAT":
            nat_type = RESTRICTED
        else:
            nat_type = SYMMETRIC

        def on_bootstrap_complete(resp):
            logger.info("bootstrap complete")
            task.LoopingCall(mserver.get_messages, mlistener).start(3600)
            task.LoopingCall(check_unfunded_for_payment, db, libbitcoin_client, nlistener, TESTNET).start(600)

        if I2P:
            # protocol = OpenBazaarI2PProtocol()...
        else:
            protocol = OpenBazaarProtocol(db, (ip_address, port), nat_type, testnet=TESTNET,
                                      relaying=True if nat_type == FULL_CONE else False)

        # kademlia
        storage = ForgetfulStorage() if TESTNET else PersistentStorage(db.get_database_path())
        relay_node = None
        if nat_type != FULL_CONE:
            for seed in SEEDS:
                try:
                    relay_node = (socket.gethostbyname(seed[0].split(":")[0]),
                                  28469 if TESTNET else 18469)
                    break
                except socket.gaierror:
                    pass

        try:
            kserver = Server.loadState(DATA_FOLDER + 'cache.pickle', ip_address, port, protocol, db,
                                       nat_type, relay_node, on_bootstrap_complete, storage)
        except Exception:
            node = Node(keys.guid, ip_address, port, keys.verify_key.encode(),
                        relay_node, nat_type, Profile(db).get().vendor)
            protocol.relay_node = node.relay_node
            kserver = Server(node, db, keys.signing_key, KSIZE, ALPHA, storage=storage)
            kserver.protocol.connect_multiplexer(protocol)
            kserver.bootstrap(kserver.querySeed(SEEDS)).addCallback(on_bootstrap_complete)
        kserver.saveStateRegularly(DATA_FOLDER + 'cache.pickle', 10)
        protocol.register_processor(kserver.protocol)

        # market
        mserver = network.Server(kserver, keys.signing_key, db)
        mserver.protocol.connect_multiplexer(protocol)
        protocol.register_processor(mserver.protocol)

        looping_retry(reactor.listenUDP, port, protocol)

        interface = "0.0.0.0" if ALLOWIP not in ("127.0.0.1", "0.0.0.0") else ALLOWIP

        # websockets api
        authenticated_sessions = []
        ws_api = WSFactory(mserver, kserver, only_ip=ALLOWIP)
        ws_factory = AuthenticatedWebSocketFactory(ws_api)
        ws_factory.authenticated_sessions = authenticated_sessions
        ws_factory.protocol = AuthenticatedWebSocketProtocol
        if SSL:
            reactor.listenSSL(WSPORT, ws_factory,
                              ChainedOpenSSLContextFactory(SSL_KEY, SSL_CERT), interface=interface)
        else:
            reactor.listenTCP(WSPORT, ws_factory, interface=interface)

        # rest api
        rest_api = RestAPI(mserver, kserver, protocol, username, password,
                           authenticated_sessions, only_ip=ALLOWIP)
        if SSL:
            reactor.listenSSL(RESTPORT, rest_api,
                              ChainedOpenSSLContextFactory(SSL_KEY, SSL_CERT), interface=interface)
        else:
            reactor.listenTCP(RESTPORT, rest_api, interface=interface)

        # blockchain
        if TESTNET:
            libbitcoin_client = LibbitcoinClient(LIBBITCOIN_SERVER_TESTNET, log=Logger(service="LibbitcoinClient"))
        else:
            libbitcoin_client = LibbitcoinClient(LIBBITCOIN_SERVER, log=Logger(service="LibbitcoinClient"))
        heartbeat_server.libbitcoin = libbitcoin_client

        # listeners
        nlistener = NotificationListenerImpl(ws_api, db)
        mserver.protocol.add_listener(nlistener)
        mlistener = MessageListenerImpl(ws_api, db)
        mserver.protocol.add_listener(mlistener)
        blistener = BroadcastListenerImpl(ws_api, db)
        mserver.protocol.add_listener(blistener)

        protocol.set_servers(ws_api, libbitcoin_client)

        if first_startup:
            heartbeat_server.push(json.dumps({
                "status": "GUID generation complete",
                "username": username,
                "password": password
            }))

        heartbeat_server.set_status("online")

        logger.info("startup took %s seconds" % str(round(time.time() - args[7], 2)))

        def shutdown():
            logger.info("shutting down server")
            for vendor in protocol.vendors.values():
                db.vendors.save_vendor(vendor.id.encode("hex"), vendor.getProto().SerializeToString())
            PortMapper().clean_my_mappings(PORT)
            protocol.shutdown()

        reactor.addSystemEventTrigger('before', 'shutdown', shutdown)

    # database
    db = Database(TESTNET)

    # client authentication
    username, password = get_credentials(db)

    # heartbeat server
    interface = "0.0.0.0" if ALLOWIP not in ("127.0.0.1", "0.0.0.0") else ALLOWIP
    heartbeat_server = HeartbeatFactory(only_ip=ALLOWIP)
    if SSL:
        reactor.listenSSL(HEARTBEATPORT, WebSocketFactory(heartbeat_server),
                          ChainedOpenSSLContextFactory(SSL_KEY, SSL_CERT), interface=interface)
    else:
        reactor.listenTCP(HEARTBEATPORT, WebSocketFactory(heartbeat_server), interface=interface)

    # key generation
    KeyChain(db, start_server, heartbeat_server)

    reactor.run()

if __name__ == "__main__":
    # pylint: disable=anomalous-backslash-in-string
    class OpenBazaard(Daemon):
        def run(self, *args):
            run(*args)

    class Parser(object):
        def __init__(self, daemon):
            self.daemon = daemon
            parser = argparse.ArgumentParser(
                description='OpenBazaar-Server v0.1.2',
                usage='''
    python openbazaard.py <command> [<args>]
    python openbazaard.py <command> --help

commands:
    start            start the OpenBazaar server
    stop             shutdown the server and disconnect
    restart          restart the server
''')
            parser.add_argument('command', help='Execute the given command')
            args = parser.parse_args(sys.argv[1:2])
            if not hasattr(self, args.command):
                parser.print_help()
                exit(1)
            getattr(self, args.command)()

        def start(self):

            parser = argparse.ArgumentParser(
                description="Start the OpenBazaar server",
                usage="python openbazaard.py start [<args>]"
            )
            parser.add_argument('-d', '--daemon', action='store_true',
                                help="run the server in the background as a daemon")
            parser.add_argument('-t', '--testnet', action='store_true', help="use the test network")
            parser.add_argument('-l', '--loglevel', default="info",
                                help="set the logging level [debug, info, warning, error, critical]")
            parser.add_argument('-p', '--port', help="set the network port")
            parser.add_argument('-a', '--allowip', default="127.0.0.1",
                                help="only allow api connections from this ip")
            parser.add_argument('-r', '--restapiport', help="set the rest api port", default=18469)
            parser.add_argument('-w', '--websocketport', help="set the websocket api port", default=18466)
            parser.add_argument('-b', '--heartbeatport', help="set the heartbeat port", default=18470)
            parser.add_argument('--pidfile', help="name of the pid file", default="openbazaard.pid")
            parser.add_argument('--i2p', action='store_true', help="use the i2p networking layer")
            args = parser.parse_args(sys.argv[2:])

            self.print_splash_screen()

            unix = ("linux", "linux2", "darwin")

            if args.port:
                port = int(args.port)
            else:
                port = 18467 if not args.testnet else 28467
            if args.daemon and platform.system().lower() in unix:
                self.daemon.pidfile = "/tmp/" + args.pidfile
                self.daemon.start(args.testnet, args.loglevel, port, args.allowip,
                                  int(args.restapiport), int(args.websocketport),
                                  int(args.heartbeatport), args.i2p, time.time())
            else:
                run(args.testnet, args.loglevel, port, args.allowip,
                    int(args.restapiport), int(args.websocketport),
                    int(args.heartbeatport), time.time())

        def stop(self):
            # pylint: disable=W0612
            parser = argparse.ArgumentParser(
                description="Shutdown the server and disconnect",
                usage='''usage:
        python openbazaard.py stop''')
            args = parser.parse_args(sys.argv[2:])
            print "OpenBazaar server stopping..."
            self.daemon.stop()

        def restart(self):
            # pylint: disable=W0612
            parser = argparse.ArgumentParser(
                description="Restart the server",
                usage='''usage:
        python openbazaard.py restart''')
            parser.parse_args(sys.argv[2:])
            print "Restarting OpenBazaar server..."
            self.daemon.restart()

        @staticmethod
        def print_splash_screen():
            OKBLUE = '\033[94m'
            ENDC = '\033[0m'
            print "________             " + OKBLUE + "         __________" + ENDC
            print "\_____  \ ______   ____   ____" + OKBLUE + \
                  "\______   \_____  _____________  _____ _______" + ENDC
            print " /   |   \\\____ \_/ __ \ /    \\" + OKBLUE +\
                  "|    |  _/\__  \ \___   /\__  \ \__  \\\_  __ \ " + ENDC
            print "/    |    \  |_> >  ___/|   |  \    " + OKBLUE \
                  + "|   \ / __ \_/    /  / __ \_/ __ \|  | \/" + ENDC
            print "\_______  /   __/ \___  >___|  /" + OKBLUE + "______  /(____  /_____ \(____  (____  /__|" + ENDC
            print "        \/|__|        \/     \/  " + OKBLUE + "     \/      \/      \/     \/     \/" + ENDC
            print
            print "OpenBazaar Server v0.1 starting..."

    Parser(OpenBazaard('/tmp/openbazaard.pid'))
