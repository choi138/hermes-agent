# SQLite lock diagnostics

SessionDB and stores using `hermes_cli.sqlite_util.open_db` emit local
`sqlite_diagnostic` JSON warnings for completed operations lasting at least one
second, busy/locked errors, and operations or open transactions lasting five
seconds. Ongoing records repeat every five seconds. Logging happens on a daemon
watchdog, outside the database writer thread; the queue retains at most 256
completed records. No process signals, database probes, or external telemetry
are used. Profile routing retains the connection's creating profile even though
the watchdog logs on another thread. Other raw sqlite3 connections are outside
this instrumentation.

## Read the evidence

1. Match `db`, `pid`, `connection`, and `transaction` across records.
2. `phase=begin_wait` with `held_ms=null` identifies a writer trying to acquire a
   lock. `begin_wait_ms` includes SQLite execution and waiting; it is not evidence
   that this connection already owns the lock.
3. A successful explicit BEGIN IMMEDIATE/EXCLUSIVE establishes the start of
   `held_ms`. `acquisition_sql` and `acquisition_caller` identify that operation;
   `last_sql`, `last_caller`, and `last_sql_ms` identify the most recent successful
   statement. An implicit transaction's holding time is only a lower bound after
   the first successfully observed DML statement, not the instant of acquisition.
4. `phase=transaction_idle` and `idle_ms` show time spent outside an observed
   SQLite operation while the transaction remains open. `operation_ms` and
   `thread_cpu_ms` on completed operations separate elapsed time from CPU time;
   low CPU time alone cannot distinguish lock waits, disk waits, or scheduling.
5. Slow `phase=commit` records distinguish committing from acquiring or executing
   a statement. Correlate timestamps with host I/O metrics to investigate disk
   saturation. Linux events include `host`: CPU total/iowait tick counters,
   CPU/I/O PSI averages over ten seconds and total stall counters (if available),
   and this process's cumulative I/O counters. Compare two samples' deltas to
   identify I/O pressure and whether Hermes itself is doing the I/O. These are
   best-effort kernel snapshots taken by the watchdog when emitting a batch,
   not per-SQL measurements. A log cannot prove which external workload delayed
   the disk. Other operating systems omit this Linux-only snapshot.

SQL literals, quoted identifiers, numbers, and comments are replaced before
logging or hashing. Bound parameters and exception messages are never logged.
Caller metadata contains function names and line numbers, not local variables.
`sql_id` identifies the normalized statement shape.

Coverage is intentionally bounded: scripts report whole-script time; fetches,
custom cursor factories and cursor methods chosen by custom Connection subclasses
are not timed. CTE writes and unusual BEGIN spelling may lack a holding-time
estimate. A slow operation still reports its caller and normalized SQL. Other
processes need this code too to attribute their own writers; absent holder records
do not prove there is no holder. Metadata is diagnostic, not an OS lock detector.

Follow-up status and authorization reads no longer begin write transactions.
Reconciliation still performs owner probes outside a transaction, acquiring a
writer only when recovery updates are needed. Fresh databases initialize the
schema lazily; that initialization still needs SQLite's schema/write locks.
Recovery and admission updates retain their token/phase predicates and atomicity.

For diagnostic searches, use relevant project/log directories and file types;
exclude databases, backups and binaries. Whole-home content scans can saturate
storage even when read-only. This guidance is not an enforced I/O limiter.
