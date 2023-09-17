
from nintendo.common import crypto, util, socket, websocket, scheduler

import itertools
import hashlib
import hmac
import struct
import random
import zlib

import logging
logger = logging.getLogger(__name__)

TYPE_SYN = 0
TYPE_CONNECT = 1
TYPE_DATA = 2
TYPE_DISCONNECT = 3
TYPE_PING = 4

FLAG_ACK = 1
FLAG_RELIABLE = 2
FLAG_NEED_ACK = 4
FLAG_HAS_SIZE = 8
FLAG_MULTI_ACK = 0x200

#I ran into issues when I was missing a support
#flag, so I'm just setting all bits to 1 here
SUPPORT_ALL = 0xFFFFFFFF

OPTION_SUPPORT = 0
OPTION_CONNECTION_SIG = 1
OPTION_FRAGMENT = 2
OPTION_3 = 3
OPTION_4 = 4
OPTION_CONNECTION_SIG_LITE = 0x80


def decode_options(data):
	pos = 0
	options = {}
	while pos < len(data):
		if len(data) - pos < 2:
			logger.error("(Opt) Only one byte left in option data")
			return

		type = data[pos]
		length = data[pos + 1]
		pos += 2

		if len(data) - pos < length:
			logger.error("(Opt) Option length is greater than data size")
			return

		if type == OPTION_SUPPORT:
			if length != 4:
				logger.error("(Opt) Invalid option length in OPTION_SUPPORT")
				return
			options[type] = struct.unpack_from("<I", data, pos)[0]

		elif type in [OPTION_CONNECTION_SIG, OPTION_CONNECTION_SIG_LITE]:
			if length != 16:
				logger.error("(Opt) Invalid option length in OPTION_CONNECTION_SIG")
				return
			options[type] = data[pos : pos + length]

		elif type in [OPTION_FRAGMENT, OPTION_4]:
			if length != 1:
				logger.error("(Opt) Invalid option length in %s",
							   "OPTION_FRAGMENT" if type == OPTION_FRAGMENT else OPTION_4)
				return
			options[type] = data[pos]

		elif type == OPTION_3:
			if length != 2:
				logger.error("(Opt) Invalid option length in OPTION_3")
				return
			options[type] = struct.unpack_from("<H", data, pos)[0]

		else:
			logger.error("(Opt) Unrecognized option type %i", type)
			return
		
		pos += length
	return options


class PRUDPPacket:
	def __init__(self, type=None, flags=None):
		self.type = type
		self.flags = flags
		
		self.source_port = None
		self.source_type = None
		self.dest_port = None
		self.dest_type = None
		self.packet_id = None
		self.signature = None
		self.fragment_id = 0
		self.multi_ack_version = 0
		self.payload = b""
		
	def __repr__(self):
		return "<PRUDPPacket type=%i flags=%03X seq=%s frag=%s>" %(self.type, self.flags, self.packet_id, self.fragment_id)
	
	
class PRUDPMessageV0:
	def __init__(self, client, settings):
		self.client = client
		self.signature_version = settings.get("prudp_v0.signature_version")
		self.flags_version = settings.get("prudp_v0.flags_version")
		self.checksum_version = settings.get("prudp_v0.checksum_version")
		self.reset()
		
	def reset(self):
		self.buffer = b""
	def signature_size(self): return 4
	
	def checksum_size(self):
		if self.checksum_version == 0:
			return 4
		return 1
		
	def calc_checksum(self, data):
		if self.checksum_version == 0:
			data = data.ljust((len(data) + 3) & ~3, b"\0")
			base = self.client.signature_base & 0xFF
			words = struct.unpack("<%iI" %(len(data) // 4), data)
			return (base + sum(words)) & 0xFFFFFFFF

		else:
			words = struct.unpack_from("<%iI" %(len(data) // 4), data)
			temp = sum(words) & 0xFFFFFFFF
			
			checksum = self.client.signature_base
			checksum += sum(data[len(data) & ~3:])
			checksum += sum(struct.pack("<I", temp))
			return checksum & 0xFF
		
	def calc_data_signature(self, packet):
		data = packet.payload
		if self.signature_version == 0:
			data = self.client.secure_key + struct.pack("<HB", packet.packet_id, packet.fragment_id) + data

		if data:
			return hmac.HMAC(self.client.signature_key, data, digestmod="md5").digest()[:4]
		return struct.pack("<I", 0x12345678)
		
	def calc_packet_signature(self, packet, signature):
		if packet.type == TYPE_DATA: return self.calc_data_signature(packet)
		if packet.type == TYPE_DISCONNECT and self.signature_version == 0:
			return self.calc_data_signature(packet)
		return signature if signature else bytes(4)
		
	def header_format(self):
		if self.flags_version == 0:
			return "<BBBB4sH"
		return "<BBHB4sH"
		
	def encode(self, packet):
		packet.flags &= ~FLAG_HAS_SIZE

		if self.flags_version == 0:
			type_field = packet.type | (packet.flags << 3)
		else:
			type_field = packet.type | (packet.flags << 4)
		
		header = struct.pack(self.header_format(),
			packet.source_port | (packet.source_type << 4),
			packet.dest_port | (packet.dest_type << 4),
			packet.type | (packet.flags << 4),
			self.client.session_id,
			self.calc_packet_signature(packet, self.client.server_signature),
			packet.packet_id
		)
		
		options = self.encode_options(packet)
		data = header + options + packet.payload
		if self.checksum_size() == 1:
			data += struct.pack("<B", self.calc_checksum(data))
		elif self.checksum_size() == 4:
			data += struct.pack("<I", self.calc_checksum(data))
		return data
		
	def encode_options(self, packet):
		options = b""
		if packet.type in [TYPE_SYN, TYPE_CONNECT]:
			options += packet.signature
		if packet.type == TYPE_DATA:
			options += struct.pack("<B", packet.fragment_id)
		if packet.flags & FLAG_HAS_SIZE:
			options += struct.pack("<H", len(packet.payload))
		return options
		
	def decode(self, data):
		self.buffer += data
	
		packets = []
		while self.buffer:
			if len(self.buffer) < 12: return packets
			
			source, dest, type_flags, session_id, signature, packet_id = \
				struct.unpack_from(self.header_format(), self.buffer)
				
			packet = PRUDPPacket()
			packet.source_type = source >> 4
			packet.source_port = source & 0xF
			packet.dest_type = dest >> 4
			packet.dest_port = dest & 0xF
			packet.packet_id = packet_id

			if self.flags_version == 0:
				packet.flags = type_flags >> 3
				packet.type = type_flags & 7
			else:
				packet.flags = type_flags >> 4
				packet.type = type_flags & 0xF

			offset = 11
			if packet.type in [TYPE_SYN, TYPE_CONNECT]:
				if len(self.buffer) < offset + 4: return packets
				packet.signature = self.buffer[offset : offset + 4]
				offset += 4

			if packet.type == TYPE_DATA:
				if len(self.buffer) < offset + 1: return packets
				packet.fragment_id = self.buffer[offset]
				offset += 1

			if packet.flags & FLAG_HAS_SIZE:
				if len(self.buffer) < offset + 2: return packets
				payload_size = struct.unpack_from("<H", self.buffer, offset)[0]
				offset += 2
			else:
				payload_size = len(self.buffer) - offset - self.checksum_size()

			if len(self.buffer) < offset + payload_size + self.checksum_size():
				return packets

			if self.checksum_size() == 1:
				checksum = self.buffer[offset + payload_size]
			elif self.checksum_size() == 4:
				checksum = struct.unpack_from("<I", self.buffer, offset + payload_size)

			if self.calc_checksum(self.buffer[:offset + payload_size]) != checksum:
				logger.error("(V0) Invalid checksum")
				self.reset()
				return packets
			packet.payload = self.buffer[offset : offset + payload_size]
			
			if signature != self.calc_packet_signature(packet, self.client.client_signature):
				logger.error("(V0) Invalid packet signature")
				self.reset()
				return packets
			
			self.buffer = self.buffer[offset + payload_size + self.checksum_size():]
			packets.append(packet)
		return packets

	
class PRUDPMessageV1:
	def __init__(self, client, settings):
		self.client = client
		self.settings = settings
		self.reset()
		
	def reset(self):
		self.buffer = b""
	def signature_size(self): return 16

	def calc_packet_signature(self, header, options, signature, payload):
		mac = hmac.HMAC(self.client.signature_key)
		mac.update(header[4:])
		mac.update(self.client.secure_key)
		mac.update(struct.pack("<I", self.client.signature_base))
		mac.update(signature)
		mac.update(options)
		mac.update(payload)
		return mac.digest()

	def encode(self, packet):
		packet.flags |= FLAG_HAS_SIZE
		options = self.encode_options(packet)
		header = self.encode_header(packet, len(options))
		checksum = self.calc_packet_signature(header, options, self.client.server_signature, packet.payload)
		return b"\xEA\xD0" + header + checksum + options + packet.payload
		
	def encode_header(self, packet, option_size):
		return struct.pack("<BBHBBHBBH",
			1, #PRUDP version
			option_size,
			len(packet.payload),
			packet.source_port | (packet.source_type << 4),
			packet.dest_port | (packet.dest_type << 4),
			packet.type | (packet.flags << 4),
			self.client.session_id, packet.multi_ack_version, packet.packet_id
		)
		
	def encode_options(self, packet):
		options = b""
		if packet.type in [TYPE_SYN, TYPE_CONNECT]:
			options += struct.pack("<BBI", OPTION_SUPPORT, 4, SUPPORT_ALL)
			options += struct.pack("<BB16s", OPTION_CONNECTION_SIG, 16, packet.signature)
			if packet.type == TYPE_CONNECT:
				options += struct.pack("<BBH", OPTION_3, 2, random.randint(0, 0xFFFF))
			options += struct.pack("<BBB", OPTION_4, 1, 0)
		elif packet.type == TYPE_DATA:
			options += struct.pack("<BBB", OPTION_FRAGMENT, 1, packet.fragment_id)
		return options
		
	def decode(self, data):
		self.buffer += data

		packets = []
		while self.buffer:
			if len(self.buffer) < 30: return packets
			if self.buffer[:2] != b"\xEA\xD0":
				logger.error("(V1) Invalid magic number")
				self.reset()
				return packets
		
			packet = PRUDPPacket()

			header = self.buffer[2 : 14]
			checksum = self.buffer[14 : 30]
			
			version, option_size, payload_size, source, dest, type_flags, session_id, \
				packet.multi_ack_version, packet_id = struct.unpack("<BBHBBHBBH", header)

			if version != 1:
				logger.error("(V1) Version check failed")
				self.reset()
				return packets
			if packet.multi_ack_version > 1:
				logger.error("(V1) Unrecognized aggregate ack version: %i" %packet.multi_ack_version)
				self.reset()
				return packets
			if len(self.buffer) < 30 + option_size + payload_size:
				return packets
				
			packet.source_type = source >> 4
			packet.source_port = source & 0xF
			packet.dest_type = dest >> 4
			packet.dest_port = dest & 0xF
			packet.flags = type_flags >> 4
			packet.type = type_flags & 0xF
			packet.packet_id = packet_id
			
			option_data = self.buffer[30 : 30 + option_size:]
			options = decode_options(option_data)
			if options is None:
				self.reset()
				return packets
			
			if packet.type in [TYPE_SYN, TYPE_CONNECT]:
				if OPTION_CONNECTION_SIG not in options:
					logger.error("(V1) Expected connection signature in SYN/CONNECT packet")
					self.reset()
					return packets
				packet.signature = options[OPTION_CONNECTION_SIG]
			elif packet.type == TYPE_DATA:
				if OPTION_FRAGMENT not in options:
					logger.error("(V1) Expected fragment id in DATA packet")
					self.reset()
					return packets
				packet.fragment_id = options[OPTION_FRAGMENT]
			
			packet.payload = self.buffer[30 + option_size : 30 + option_size + payload_size]

			if self.calc_packet_signature(header, option_data, self.client.client_signature, packet.payload) != checksum:
				logger.error("(V1) Invalid packet signature")
				self.reset()
				return packets
			
			self.buffer = self.buffer[30 + option_size + payload_size:]
			packets.append(packet)
		return packets

		
class PRUDPLiteMessage:
	def __init__(self, client, settings):
		self.client = client
		self.settings = settings
		self.reset()
		
	def reset(self):
		self.buffer = b""
	def signature_size(self): return 16
		
	def encode(self, packet):
		options = self.encode_options(packet)
		header = self.encode_header(packet, len(options))
		return header + options + packet.payload
		
	def encode_header(self, packet, option_size):
		return struct.pack("<BBHBBBBHH",
			0x80, option_size,
			len(packet.payload),
			(packet.source_type << 4) | packet.dest_type,
			packet.source_port,
			packet.dest_port,
			packet.fragment_id,
			packet.type | (packet.flags << 4),
			packet.packet_id
		)
		
	def encode_options(self, packet):
		options = b""
		if packet.type in [TYPE_SYN, TYPE_CONNECT]:
			options += struct.pack("<BBI", OPTION_SUPPORT, 4, SUPPORT_ALL)
		if packet.type == TYPE_CONNECT:
			options += struct.pack("<BB16s", OPTION_CONNECTION_SIG_LITE, 16, packet.signature)
		return options
		
	def decode(self, data):
		self.buffer += data

		packets = []
		while self.buffer:
			if len(self.buffer) < 12: return packets
		
			packet = PRUDPPacket()
			
			magic, option_size, payload_size, stream_types, source_port, dest_port, \
				fragment_id, type_flags, packet_id = struct.unpack_from("<BBHBBBBHH", self.buffer)
			
			if magic != 0x80:
				logger.error("(Lite) Invalid magic number")
				self.reset()
				return packets
			if len(self.buffer) < 12 + option_size + payload_size:
				return packets
				
			packet.source_type = stream_types >> 4
			packet.dest_type = stream_types & 0xF
			packet.source_port = source_port
			packet.dest_port = dest_port
			packet.fragment_id = fragment_id
			packet.flags = type_flags >> 4
			packet.type = type_flags & 0xF
			packet.packet_id = packet_id
			
			option_data = self.buffer[12 : 12 + option_size:]
			options = decode_options(option_data)
			if options is None:
				self.reset()
				return packets
			
			if packet.type == TYPE_SYN:
				if OPTION_CONNECTION_SIG not in options:
					logger.error("(Lite) Expected connection signature in SYN packet")
					self.reset()
					return packets
				packet.signature = options[OPTION_CONNECTION_SIG]
			
			packet.payload = self.buffer[12 + option_size : 12 + option_size + payload_size]
			self.buffer = self.buffer[12 + option_size + payload_size:]
			packets.append(packet)
		return packets

		
class RC4Encryption:
	def __init__(self, key):
		self.rc4enc = crypto.RC4(key)
		self.rc4dec = crypto.RC4(key)
		
	def set_key(self, key):
		self.rc4enc.set_key(key)
		self.rc4dec.set_key(key)
		
	def encrypt(self, data): return self.rc4enc.crypt(data)
	def decrypt(self, data): return self.rc4dec.crypt(data)
	

class DummyEncryption:
	def set_key(self, key): pass
	def encrypt(self, data): return data
	def decrypt(self, data): return data
	
	
class ZlibCompression:
	def compress(self, data):
		compressed = zlib.compress(data)
		ratio = int(len(data) / len(compressed) + 1)
		return bytes([ratio]) + compressed
		
	def decompress(self, data):
		decompressed = zlib.decompress(data[1:])
		ratio = int(len(decompressed) / (len(data) - 1) + 1)
		if ratio != data[0]:
			logger.warning(
				"Unexpected compression ratio (expected %i, got %i)",
				ratio, data[0]
			)
		return decompressed
		
		
class DummyCompression:
	def compress(self, data): return data
	def decompress(self, data): return data
	

class PRUDPClient:

	DISCONNECTED = 0
	CONNECTING = 1
	CONNECTED = 2
	DISCONNECTING = 3

	DEFAULT_KEY = b"CD&ML"

	def __init__(self, settings):
		self.settings = settings
		self.transport_type = settings.get("prudp.transport")
		self.stream_type = settings.get("prudp.stream_type")
		self.fragment_size = settings.get("prudp.fragment_size")
		self.resend_timeout = settings.get("prudp.resend_timeout")
		self.ping_timeout = settings.get("prudp.ping_timeout")
		self.silence_timeout = settings.get("prudp.silence_timeout")
		self.use_compression = settings.get("prudp.compression")

		access_key = settings.get("server.access_key")
		self.signature_key = hashlib.md5(access_key).digest()
		self.signature_base = sum(access_key)
		
		self.server_port = 1
		if self.transport_type == settings.TRANSPORT_UDP:
			if settings.get("prudp.version") == 0:
				self.packet_encoder = PRUDPMessageV0(self, settings)
			else:
				self.packet_encoder = PRUDPMessageV1(self, settings)
			self.encryption = RC4Encryption(self.DEFAULT_KEY)
			self.client_port = 0xF
		else:
			self.packet_encoder = PRUDPLiteMessage(self, settings)
			self.encryption = DummyEncryption()
			self.client_port = 0x1F
			
		if settings.get("prudp.compression") == 0:
			self.compression = DummyCompression()
		else:
			self.compression = ZlibCompression()
			
		self.syn_packet = PRUDPPacket(TYPE_SYN, FLAG_NEED_ACK)
		self.syn_packet.signature = bytes(16)
		self.connect_packet = PRUDPPacket(TYPE_CONNECT, FLAG_RELIABLE | FLAG_NEED_ACK)
		self.state = self.DISCONNECTED
		
	def set_secure_key(self, key):
		self.encryption.set_key(key)
		self.secure_key = key
		
	def is_connected(self): return self.state == self.CONNECTED
	def client_address(self): return self.s.client_address()
	def server_address(self): return self.s.server_address()
		
	def connect(self, host, port, payload=b""):
		if self.state != self.DISCONNECTED:
			raise RuntimeError("Socket was not disconnected")
	
		logger.info("Connecting to %s:%i", host, port)
		self.state = self.CONNECTING

		self.encryption.set_key(self.DEFAULT_KEY)
		self.secure_key = b""
		
		self.server_signature = b""
		self.client_signature = b""
		self.connect_response = b""

		self.packets = []
		self.packet_queue = {}
		self.fragment_buffer = b""
		self.packet_id_out = itertools.count()
		self.packet_id_in = 1
		self.session_id = 0
		
		self.packet_encoder.reset()
		if self.transport_type == self.settings.TRANSPORT_UDP:
			self.s = socket.Socket(socket.TYPE_UDP)
		elif self.transport_type == self.settings.TRANSPORT_TCP:
			self.s = socket.Socket(socket.TYPE_TCP)
		else:
			self.s = websocket.WebSocket()

		if not self.s.connect(host, port):
			logger.error("Socket connection failed")
			self.state = self.DISCONNECTED
			return False
			
		self.ack_events = {}
		self.ping_event = None
		self.timeout_event = scheduler.add_timeout(self.handle_silence_timeout, self.silence_timeout)
		self.socket_event = scheduler.add_socket(self.handle_recv, self.s)

		self.send_packet(self.syn_packet)
		if not self.wait_ack(self.syn_packet):
			logger.error("PRUDP connection failed")
			return False
			
		self.session_id = random.randint(0, 0xFF)
		if self.transport_type == self.settings.TRANSPORT_UDP:
			self.client_signature = bytes([random.randint(0, 0xFF) for i in range(self.packet_encoder.signature_size())])
		else:
			self.client_signature = hmac.HMAC(self.signature_key, self.signature_key + self.server_signature).digest()
		self.connect_packet.signature = self.client_signature
		self.connect_packet.payload = payload

		self.send_packet(self.connect_packet)
		if not self.wait_ack(self.connect_packet):
			logger.error("PRUDP connection failed")
			return False
			
		self.ping_event = scheduler.add_timeout(self.handle_ping, self.ping_timeout, True)

		logger.info("PRUDP connection OK")
		self.state = self.CONNECTED
		return True
		
	def close(self):
		if self.state != self.DISCONNECTED:
			self.state = self.DISCONNECTING
			
			scheduler.remove(self.ping_event)
			self.ping_event = None
			
			packet = PRUDPPacket(TYPE_DISCONNECT, FLAG_RELIABLE | FLAG_NEED_ACK)
			self.send_packet(packet)
			self.wait_ack(packet)
			self.s.close()
			
			self.state = self.DISCONNECTED
			self.remove_events()
			logger.debug("(%i) PRUDP connection closed", self.session_id)
			
	def recv(self):
		if self.state != self.CONNECTED: return b""
		if self.packets:
			return self.packets.pop(0)
			
	def send(self, data):
		if self.state != self.CONNECTED:
			raise RuntimeError("Can't send data on a disconnected PRUDP socket")

		fragment_id = 1
		while data:
			if len(data) <= self.fragment_size:
				fragment_id = 0
			self.send_fragment(data[:self.fragment_size], fragment_id)
			data = data[self.fragment_size:]
			fragment_id += 1

	def send_fragment(self, data, fragment_id):
		data = self.compression.compress(data)
		data = self.encryption.encrypt(data)

		packet = PRUDPPacket(TYPE_DATA, FLAG_RELIABLE | FLAG_NEED_ACK)
		packet.fragment_id = fragment_id
		packet.payload = data
		self.send_packet(packet)
		
	def remove_events(self):
		scheduler.remove(self.socket_event)
		scheduler.remove(self.timeout_event)
		if self.ping_event:
			scheduler.remove(self.ping_event)
		for event in self.ack_events.values():
			scheduler.remove(event)
		
	def handle_recv(self, data):
		if not data:
			logger.debug("(%i) Connection was closed" %self.session_id)
			self.state = self.DISCONNECTED
			self.remove_events()
			return

		packets = self.packet_encoder.decode(data)
		for packet in packets:
			logger.debug("(%i) Packet received: %s" %(self.session_id, packet))
			if packet.flags & FLAG_ACK:
				logger.debug("(%i) Packet acknowledged: %s" %(self.session_id, packet))
				if packet.packet_id in self.ack_events:
					if packet.type == TYPE_SYN:
						self.server_signature = packet.signature
					elif packet.type == TYPE_CONNECT:
						self.connect_response = packet.payload
					scheduler.remove(self.ack_events.pop(packet.packet_id))

			elif packet.flags & FLAG_MULTI_ACK:
				if self.transport_type != self.settings.TRANSPORT_UDP or packet.multi_ack_version == 1:
					ack_id = struct.unpack_from("<H", packet.payload, 2)[0]
				else:
					ack_id = struct.unpack("<H", packet.payload)[0]
				logger.debug("(%i) Aggregate ack up to packet %i" %(self.session_id, ack_id))
				for packet_id in list(self.ack_events.keys()):
					if packet_id <= ack_id:
						scheduler.remove(self.ack_events.pop(packet_id))
						
			else:
				if packet.flags & FLAG_NEED_ACK:
					self.send_ack(packet)
					if packet.type == TYPE_DISCONNECT:
						self.send_ack(packet)
						self.send_ack(packet)
			
				if packet.packet_id >= self.packet_id_in:
					self.packet_queue[packet.packet_id] = packet
					while self.packet_id_in in self.packet_queue:
						packet = self.packet_queue.pop(self.packet_id_in)
						self.handle_packet(packet)
						self.packet_id_in += 1
				
			if self.ping_event:
				self.ping_event.reset()
			self.timeout_event.reset()
			
	def handle_packet(self, packet):
		if packet.type == TYPE_DATA:
			payload = self.encryption.decrypt(packet.payload)
			payload = self.compression.decompress(payload)
			self.fragment_buffer += payload
			if packet.fragment_id == 0:
				self.packets.append(self.fragment_buffer)
				self.fragment_buffer = b""
				
		elif packet.type == TYPE_DISCONNECT:
			logger.info("(%i) Server closed connection")
			self.state = self.DISCONNECTED
			self.remove_events()
			
	def handle_ping(self):
		packet = PRUDPPacket(TYPE_PING, FLAG_RELIABLE | FLAG_NEED_ACK)
		self.send_packet(packet)

	def handle_silence_timeout(self):
		logger.error("Connection died")
		self.state = self.DISCONNECTED
		self.remove_events()
		
	def handle_ack_timeout(self, param):
		packet, counter = param
		if counter < 3:
			logger.debug("(%i) Resending packet: %s", self.session_id, packet)
			self.send_packet_raw(packet)
			
			event = scheduler.add_timeout(self.handle_ack_timeout, self.resend_timeout, param=(packet, counter+1))
			self.ack_events[packet.packet_id] = event
		else:
			logger.error("Packet timed out")
			self.state = self.DISCONNECTED
			del self.ack_events[packet.packet_id]
			self.remove_events()
			
	def send_ack(self, packet):
		ack = PRUDPPacket(packet.type, FLAG_ACK)
		ack.packet_id = packet.packet_id
		ack.fragment_id = packet.fragment_id
			
		logger.debug("(%i) Sending ack: %s", self.session_id, ack)
		self.send_packet_raw(ack)
		
	def wait_ack(self, packet):
		while self.state != self.DISCONNECTED and packet.packet_id in self.ack_events:
			scheduler.update()
		return self.state != self.DISCONNECTED
		
	def send_packet(self, packet):
		packet.packet_id = next(self.packet_id_out)

		logger.debug("(%i) Sending packet: %s", self.session_id, packet)
		
		if packet.flags & FLAG_NEED_ACK:
			event = scheduler.add_timeout(self.handle_ack_timeout, self.resend_timeout, param=(packet, 0))
			self.ack_events[packet.packet_id] = event
		
		self.send_packet_raw(packet)
		
	def send_packet_raw(self, packet):
		packet.source_port = self.client_port
		packet.source_type = self.stream_type
		packet.dest_port = self.server_port
		packet.dest_type = self.stream_type
		self.s.send(self.packet_encoder.encode(packet))
