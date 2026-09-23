"""Library metadata generators.

Batch tools that read a launcher's own cache and write frontend metadata. They
are **not** part of the daemon and must never be imported by it (CLAUDE.md §2):
they do network and filesystem work that has no business anywhere near the
input loop, and they run for seconds at a time.

The rule is enforced rather than trusted -- ``tests/test_metadata.py`` asserts
that importing :mod:`padmode.daemon` never pulls this package in. ``padmode
metadata`` imports it lazily, inside the subcommand, which is the only place
that is allowed to.

See ``docs/metadata-generators.md`` for the design of both generators.
"""
