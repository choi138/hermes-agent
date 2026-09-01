"""Keep the quarantined M1 test-debt candidates out of every collection.

pyproject's ``testpaths`` already points at ``tests``, but an explicit
``pytest .`` or an editor's run-all would otherwise pull these in. They are
recovered A-lineage files that have not been ported yet, so several of them
fail by design here.
"""

collect_ignore_glob = ["*"]
