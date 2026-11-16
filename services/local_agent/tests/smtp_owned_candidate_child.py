"""Closed synthetic worker for process-ownership tests. This file has no network code."""
import sys
import time

_ALLOWED = {'success', 'stall_ready', 'stall_after_go', 'bad_reply'}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in _ALLOWED:
        return 20
    mode = sys.argv[1]
    # This exact fixed marker is test data, never a credential or user input.
    if sys.stdin.buffer.readline(128) != b'SYNTHETIC_CREDENTIALS\n':
        return 21
    if mode == 'stall_ready':
        time.sleep(60)
        return 22
    sys.stdout.buffer.write(b'READY\n')
    sys.stdout.buffer.flush()
    if sys.stdin.buffer.readline(128) != b'GO\n':
        return 23
    if mode == 'stall_after_go':
        time.sleep(60)
        return 24
    sys.stdout.buffer.write(b'ACCEPTED\n' if mode == 'success' else b'INVALID_SYNTHETIC_REPLY\n')
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
