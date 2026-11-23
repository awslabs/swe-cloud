# Attribution

Upstream: https://github.com/rrreeeyyy/ebschedule
Revision: 7b42b7d0009f039ab81a079b3e2820b6284a0d9d

Licenses and notices for distribution only; not installed in task containers.

## legal/FIXTURES.md

```text
# Test fixture provenance

`task/tests/test_schedules.py::test_multi_group_example` embeds a configuration
adapted from upstream `examples/04-multi-group-schedules.yaml` (ebschedule,
commit 7b42b7d0009f039ab81a079b3e2820b6284a0d9d, MIT License, see LICENSE):
region rewritten to us-east-1, resource names prefixed per test, Lambda
targets replaced with SQS ARNs. Other configurations in the tests are
written for this task, in the documented public configuration format.

```

## legal/LICENSE

```text
MIT License

Copyright (c) 2026 Ryota Yoshikawa

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

```
