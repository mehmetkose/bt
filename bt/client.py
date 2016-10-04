# -*- coding: utf-8 -*-

import random
import time
import math
import os
from collections import namedtuple
from hashlib import sha1
import asyncio

from .torrent_parser import parse
from .logger import get_logger
from .tracker import HTTPTracker, UDPTracker
from .protocol import PeerConnection
from .message import REQUEST_SIZE
from .mixins import ReprMixin


logger = get_logger()


Peer = namedtuple('Peer', ['pending_blocks', 'missing_blocks',
                           'ongoing_pieces', 'bitfield'])
PendingRequest = namedtuple('PendingRequest', ['block', 'added'])


class Block(ReprMixin):
    Missing = 0
    Pending = 1
    Retrieved = 2

    __repr_fields__ = ('piece', 'offset', 'length', 'status')

    def __init__(self, piece, offset, length):
        self.piece = piece
        self.offset = offset
        self.length = length
        self.status = Block.Missing
        self.data = None


class Piece(ReprMixin):
    __repr_fields__ = ('index', 'blocks', 'hash_value')

    def __init__(self, index, blocks, hash_value):
        self.index = index
        self.blocks = blocks
        self.hash = hash_value

    def reset(self):
        for block in self.blocks:
            block.status = Block.Missing

    def next_request(self):
        missing = [b for b in self.blocks if b.status is Block.Missing]
        if missing:
            missing[0].status = Block.Pending
            return missing[0]
        return None

    def block_received(self, offset, data):
        matches = [b for b in self.blocks if b.offset == offset]
        block = matches[0] if matches else None
        if block:
            logger.info('Block retrieved, offset {}'.format(offset))
            block.status = Block.Retrieved
            block.data = data
        else:
            logging.warning('Trying to complete a non-existing block {offset}'
                            .format(offset=offset))

    def is_complete(self):
        blocks = [b for b in self.blocks if b.status is not Block.Retrieved]
        logger.info('Pending pieces: {}'.format(len(blocks)))
        return len(blocks) is 0

    def is_hash_matching(self):
        piece_hash = bytearray(sha1(self.data).hexdigest(), 'utf-8')
        return self.hash == piece_hash

    @property
    def data(self):
        retrieved = sorted(self.blocks, key=lambda b: b.offset)
        blocks_data = [b.data for b in retrieved]
        return b''.join(blocks_data)


class DownloadManager:
    """Manager keeps track of all the pieces, connections, 
    state of the download and all the other info.
    """
    def __init__(self, torrent):
        self.torrent = torrent

        self.total_pieces = len(self.torrent.info.pieces)
        self.peers = {}
        # TODO: Come up with different data structure to store
        # states of different pieces and blocks. Probably dict or set?
        self.pending_blocks = []
        self.ongoing_pieces = []
        self.have_pieces = []
        self.missing_pieces = self.make_pieces()
        self.fd = os.open(self.torrent.name,  os.O_RDWR | os.O_CREAT)

    @property
    def complete(self):
        return len(self.have_pieces) == self.total_pieces

    @property
    def bytes_uploaded(self):
        return 0

    @property
    def bytes_downloaded(self):
        return len(self.have_pieces) * self.torrent.info.piece_length

    def on_block_complete(self, peer_id, piece_index, block_offset, data):
        logger.info('Received block offset {block_offset}'
                     ' for piece {piece_index} from peer {peer_id}'.format(
            block_offset=block_offset, piece_index=piece_index,
            peer_id=peer_id))

        self.remove_from_pending_pieces(peer_id, piece_index,
                                        block_offset, data)
        res = self.update_ongoing_pieces(peer_id, piece_index,
                                         block_offset, data)
        if res:
            self.update_have_pieces(peer_id, piece_index,
                                    block_offset, data)

    def remove_from_pending_pieces(self, peer_id,
                                   piece_index, block_offset, data):
        for index, request in enumerate(self.pending_blocks):
            if request.block.piece == piece_index and \
               request.block.offset == block_offset:
               logger.debug('Removing from pending offset: {}'.format(
                   self.pending_blocks))
               del self.pending_blocks[index]
               break

    def update_ongoing_pieces(self, peer_id,
                             piece_index, block_offset, data):
        pieces = [p for p in self.ongoing_pieces if p.index == piece_index]
        logger.debug('Checking update ongoing piece: {}'.format(len(pieces)))
        piece = pieces[0] if pieces else None
        if piece:
            piece.block_received(block_offset, data)
            if piece.is_complete():
                if piece.is_hash_matching():
                    self._write(piece)
                    self.ongoing_pieces.remove(piece)
                    return piece
                else:
                    logger.debug("Discarding the corrupt piece")
                    piece.reset()
        else:
            logger.debug("Piece doesn't exist to update")

    def update_have_piece(self, piece):
        complete = (self.total_pieces - len(self.missing_pieces) -
                    len(self.ongoing_pieces))
        logger.info('{complete} / {total} pieces downloaded {per:.3f} %'.format(
            complete=complete, total=self.total_pieces,
            per=(complete/self.total_pieces)*100))

    def make_pieces(self):
        total_pieces = len(self.torrent.info.pieces)
        total_piece_blocks = math.ceil(
            self.torrent.info.piece_length / REQUEST_SIZE)
        pieces = []
        for index, hash_value in enumerate(self.torrent.info.pieces):
            if index < (total_pieces - 1):
                blocks = [Block(index, offset * REQUEST_SIZE,
                                REQUEST_SIZE)
                          for offset in range(total_piece_blocks)]
            else:
                last_length = self.torrent.info.length % self.torrent.info.piece_length
                num_blocks = math.ceil(last_length / REQUEST_SIZE)
                blocks = [Block(index, offset * REQUEST_SIZE, REQUEST_SIZE)
                          for offset in range(num_blocks)]

                if last_length % REQUEST_SIZE > 0:
                    last_block = blocks[-1]
                    last_block.length = last_length % REQUEST_SIZE
                    block[-1] = last_block
            pieces.append(Piece(index, blocks, hash_value))
        logger.debug('Completed calculating pieces')
        return pieces

    def add_peer(self, peer_id, bitfield):
        self.peers[peer_id] = bitfield

    def next_request(self, peer_id):
        if peer_id not in self.peers:
            return None

        block = self._expired_request(peer_id)
        if not block:
            block = self._next_ongoing(peer_id)
            if not block:
                block = self._next_missing(peer_id)
        return block

    def _expired_request(self, peer_id):
        """
        """
        #logger.debug('Checking expired request')
        current = int(round(time.time() * 1000))
        for request in self.pending_blocks:
            if self.peers[peer_id][request.block.piece]:
                if request.added + self.max_pending_time < current:
                    logging.info('Re-requesting block {block} for '
                                 'piece {piece}'.format(
                                    block=request.block.offset,
                                    piece=request.block.piece))
                    # Reset expiration timer
                    request.added = current
                    return request.block
        return None

    def _next_ongoing(self, peer_id):
        #logger.debug('Checking next ongoing')
        for piece in self.ongoing_pieces:
            if self.peers[peer_id][piece.index]:
                # Is there any blocks left to request in this piece?
                block = piece.next_request()
                if block:
                    self.pending_blocks.append(
                        PendingRequest(block, int(round(time.time() * 1000))))
                    return block
        return None

    def _next_missing(self, peer_id):
        for index, piece in enumerate(self.missing_pieces):
            if self.peers[peer_id][piece.index]:
                # Move this piece from missing to ongoing
                piece = self.missing_pieces.pop(index)
                self.ongoing_pieces.append(piece)
                # The missing pieces does not have any previously requested
                # blocks (then it is ongoing).
                return piece.next_request()
        return None

    def _write(self, piece):
        pos = piece.index * self.torrent.info.piece_length
        os.lseek(self.fd, pos, os.SEEK_SET)
        os.write(self.fd, piece.data)

    def close(self):
        if self.fd:
            os.close(self.fd)


class Client:
    def __init__(self):
        self.tracker = None
        self.available_peers = asyncio.Queue()
        self.peers = []
        self.download_manager = None
        self.abort = False

    def on_block_complete(self, peer_id,
                          piece_index, block_offset, data):
        self.download_manager.on_block_complete(
            peer_id=peer_id,
            piece_index=piece_index,
            block_offset=block_offset,
            data=data)

    async def download(self, path):
        torrent = parse(path)
        torrent.print_all_info()

        if torrent.announce.startswith(b'http'):
            tracker = HTTPTracker(url=torrent.announce,
                                  size=torrent.info.length,
                                  info_hash=torrent.hash)
            self.tracker = tracker
            resp = await tracker.announce()
            self.previous = time.time()
            logger.info("Tracker Resp: {}".format(resp))
            self.download_manager = DownloadManager(torrent)
            for peer in resp.peers:
                self.available_peers.put_nowait(peer)
            self.peers = [PeerConnection(
                info_hash=torrent.hash,
                peer_id=tracker.peer_id,
                available_peers=self.available_peers,
                download_manager=self.download_manager,
                on_block_complete=self.on_block_complete)
                          for _ in range(10)]

            await self.monitor()

        elif torrent.announce.startswith(b'udp'):
            logger.info("UDP tracker isn't supported")
            exit(1)

    async def monitor(self):
        # Interval in seconds
        interval = 5 * 60

        while True:
            if self.download_manager.complete:
                logger.info('Download complete, exiting...')
                break
            elif self.abort:
                logger.info('Aborting download...')
                break

            current = time.time()
            if (self.previous + interval < current):
                response = await self.tracker.connect(
                    first=self.previous if self.previous else False,
                    uploaded=self.download_manager.bytes_uploaded,
                    downloaded=self.download_manager.bytes_downloaded)

                if response:
                    self.previous = current
                    interval = response.interval
                    self._empty_queue()
                    for peer in response.peers:
                        self.available_peers.put_nowait(peer)
            else:
                await asyncio.sleep(0.1)
        self.stop()

    def _empty_queue(self):
        while not self.available_peers.empty():
            self.available_peers.get_nowait()

    def stop(self):
        self.abort = True
        [peer.stop() for peer in self.peers]
        self.download_manager.close()
        self.tracker.close()
