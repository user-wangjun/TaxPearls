"""Short-lived resource-limited validator with no inherited application credentials."""
import base64
import json
import os
import socket
import sys


def main():
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (768*1024*1024, 768*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    def no_network(*args, **kwargs):
        raise PermissionError('material validation does not use network')
    socket.create_connection = no_network
    socket.socket.connect = no_network
    from .material_formats import expand
    from .input_errors import InputError
    try:
        payload = json.loads(sys.stdin.buffer.read(72*1024*1024))
        files = [(name, base64.b64decode(raw, validate=True)) for name, raw in payload['files']]
        result = expand(files)
        answer = {'ok': True, 'files': [[name, base64.b64encode(raw).decode() if payload.get('expand') else None, info]
                                      for name, raw, info in result]}
    except InputError as exc:
        answer = {'ok': False, 'message': str(exc)}
    except Exception:
        answer = {'ok': False, 'message': '材料结构检查失败，未接收此批文件。'}
    sys.stdout.write(json.dumps(answer, ensure_ascii=False))


if __name__ == '__main__':
    main()
