import json
import os
import socket


def seam(name, **facts):
    path = os.environ.get('SEAM_SOCKET')
    if not path:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(path)
        connection.sendall((json.dumps({'seam': name, **facts}) + '\n').encode())
        connection.recv(64)
