"""Hidden local credential provisioning. Never opens a network connection."""
import getpass
import os
from pathlib import Path
import stat

PATH = Path(__file__).resolve().parents[1] / '.env.m8-test'
VARIABLE = 'M8_GEMINI_TEST_API_KEY'


def load_test_key(path=PATH):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('M8 credential file must be owned by this user with mode 0600')
    text = path.read_text().strip()
    prefix = VARIABLE + '='
    if not text.startswith(prefix) or not text[len(prefix):] or '\n' in text:
        raise ValueError('Invalid M8 credential file')
    return text[len(prefix):]


def main():
    import sys
    if not sys.stdin.isatty():
        raise SystemExit('Run locally in an interactive terminal; hidden input is required.')
    key = getpass.getpass('Dedicated M8 Gemini test key (hidden): ').strip()
    if not key or not key.isascii() or any(c.isspace() for c in key):
        raise SystemExit('Invalid key; nothing stored.')
    fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(VARIABLE + '=' + key + '\n')
    print('Dedicated M8 test credential stored with mode 0600. Network access remains disabled.')


if __name__ == '__main__':
    main()
