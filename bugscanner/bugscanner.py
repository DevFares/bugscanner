import sys
import ssl
import time
import queue
import socket
import argparse
import requests
import threading

CN = "\033[K"
G1 = "\033[32;1m"
CC = "\033[0m"

lock = threading.RLock()

def get_value_from_list(data, index, default=""):
	try:
		return data[index]
	except IndexError:
		return default

def log(value):
	with lock:
		print(f"{CN}{value}{CC}")

def log_replace(value):
	sys.stdout.write(f"{CN}{value}{CC}\r")
	sys.stdout.flush()

class BugScanner:
	scanned = {
		"direct": {},
		"ssl": {},
		"proxy": {},
	}
	direct_response_scanned = {}
	http_proxy_response_scanned = {}

	def print_result(self, host, hostname, status_code=None, server=None, sni=None, color=""):
		if not color and (server in ["AkamaiGHost", "Varnish", "AmazonS3"] or sni == "True"):
			color = G1

		host = f"{host:<15}"
		hostname = f"  {hostname}"
		sni = f"  {sni:<4}" if sni is not None else ""
		server = f"  {server:<20}" if server is not None else ""
		status_code = f"  {status_code:<4}" if status_code is not None else ""

		log(f"{color}{host}{status_code}{server}{sni}{hostname}")

	def request(self, method, hostname, port, *args, **kwargs):
		try:
			url = ("https" if port == 443 else "http") + "://" + (hostname if port == 443 else f"{hostname}:{port}")
			log_replace(f"{method} {url}")
			return requests.request(method, url, *args, **kwargs)
		except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
			return None

	def resolve(self, hostname):
		try:
			cname, hostname_list, host_list = socket.gethostbyname_ex(hostname)
		except (socket.gaierror, socket.herror):
			return

		for i in range(len(hostname_list)):
			yield get_value_from_list(host_list, i, host_list[-1]), hostname_list[i]

		yield host_list[-1], cname

	def get_direct_response(self, host, hostname, port):
		with lock:
			if host in self.scanned["direct"]:
				return self.scanned["direct"][host]

		response = self.request("HEAD", hostname, port, timeout=5)
		if response is not None:
			status_code = response.status_code
			server = response.headers.get("server")
		else:
			status_code = ""
			server = ""

		self.scanned["direct"][host] = {
			"status_code": status_code,
			"server": server,
		}
		return self.scanned["direct"][host]

	def get_sni_response(self, hostname):
		server_name_indication = ".".join(hostname.split(".")[0 - self.deep:])
		with lock:
			if server_name_indication in self.scanned["ssl"]:
				return self.scanned["ssl"][server_name_indication]

		try:
			log_replace(server_name_indication)
			socket_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
			socket_client.settimeout(5)
			socket_client.connect(("httpbin.org", 443))
			socket_client = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2).wrap_socket(
				socket_client, server_hostname=server_name_indication, do_handshake_on_connect=True
			)
		except:
			response = ""
		else:
			response = "True"
		finally:
			self.scanned["ssl"][server_name_indication] = response
			return self.scanned["ssl"][server_name_indication]

	def get_proxy_response(self, hostname):
		with lock:
			if hostname in self.scanned["proxy"]:
				return self.scanned["proxy"][hostname]

		proxies = {
			"http": "http://" + self.proxy,
			"https": "http://" + self.proxy,
		}
		response = self.request(self.method.upper(), hostname, self.port, proxies=proxies, allow_redirects=False, timeout=5)
		if response is None:
			return None

		self.scanned["proxy"][hostname] = {
			"proxy": self.proxy,
			"method": self.method,
			"url": response.url,
			"status_code": response.status_code,
			"headers": response.headers,
		}
		return self.scanned["proxy"][hostname]

	def print_proxy_response(self, response):
		if response is None:
			return

		data = []
		data.append(f"{response['proxy']} -> {response['method']} {response['url']} ({response['status_code']})\n")
		for key, val in response['headers'].items():
			data.append(f"|   {key}: {val}")
		data.append("|\n\n")

		log("\n".join(data))

	def scan(self):
		while True:
			for host, hostname in self.resolve(self.queue_hostname.get()):
				if self.mode == "direct":
					response = self.get_direct_response(host, hostname, self.port)
					self.print_result(host, hostname, status_code=response["status_code"], server=response["server"])

				elif self.mode == "ssl":
					self.print_result(host, hostname, sni=self.get_sni_response(hostname))

				elif self.mode == "proxy":
					response = self.get_proxy_response(hostname)
					self.print_proxy_response(response)

			self.queue_hostname.task_done()

	def start(self, hostnames):
		if self.mode == "direct":
			self.print_result("host", "hostname", status_code="code", server="server")
			self.print_result("----", "--------", status_code="----", server="------")
		elif self.mode == "ssl":
			self.print_result("host", "hostname", sni="sni")
			self.print_result("----", "--------", sni="---")

		self.queue_hostname = queue.Queue()
		for hostname in hostnames:
			self.queue_hostname.put(hostname)

		for _ in range(min(self.threads, self.queue_hostname.qsize())):
			thread = threading.Thread(target=self.scan)
			thread.daemon = True
			thread.start()

		self.queue_hostname.join()

def main():
	parser = argparse.ArgumentParser(
		formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=52))
	parser.add_argument("filename", help="filename", type=str)
	parser.add_argument("-m", "--mode", help="direct, proxy, ssl", dest="mode", type=str, default="direct")
	parser.add_argument("-d", "--deep", help="subdomain deep", dest="deep", type=int, default=2)
	parser.add_argument("-p", "--port", help="target port", dest="port", type=int, default=80)
	parser.add_argument("-t", "--threads", help="threads", dest="threads", type=int, default=8)

	parser.add_argument("-P", "--proxy", help="proxy.example.com:8080", dest="proxy", type=str)
	parser.add_argument("-M", "--method", help="http method", dest="method", type=str, default="HEAD")

	args = parser.parse_args()
	if args.mode == "proxy" and not args.proxy:
		parser.print_help()
		return

	bugscanner = BugScanner()
	bugscanner.mode = args.mode
	bugscanner.deep = args.deep
	bugscanner.port = args.port
	bugscanner.proxy = args.proxy
	bugscanner.method = args.method
	bugscanner.threads = args.threads
	bugscanner.start(open(args.filename).read().splitlines())

if __name__ == "__main__":
	main()

