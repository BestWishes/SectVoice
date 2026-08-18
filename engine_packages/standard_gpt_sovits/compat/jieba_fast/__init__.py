"""Windows fallback exposing the jieba_fast API through regular jieba."""

from jieba import *  # noqa: F401,F403
from jieba import setLogLevel  # noqa: F401

