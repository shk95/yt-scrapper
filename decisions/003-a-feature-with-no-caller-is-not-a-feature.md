# A method with tests and no callers is invisible to every check here

**The rule.** When a capability is added, something must call it, and a test
must exercise the path that calls it — not only the capability itself.

**What went wrong without it.** `JobRepository.renew_lease` was written,
documented, type-checked and unit-tested on the day leases were introduced.
Nothing ever called it. So the lease was a deadline rather than a heartbeat: the
reaper returns any running job whose lease has expired, and a comment harvest
runs for tens of minutes against a fifteen-minute default. The first worker
keeps going while a second starts the same harvest against the same address —
two harvests, one result, twice the requests. Exactly the failure the lease was
introduced to prevent, caused by the lease.

It was found by grepping for callers while looking at something else. Not by a
test, not by the linter, not by the type checker: the tests passed, ruff was
happy, basedpyright was happy, and the behaviour was simply absent. There is no
check in this repository that would have caught it, which is why this is written
down rather than assumed.

**The same shape, caught deliberately since.** The webhook sender and the
source-health recorder both have a test asserting the *worker* invokes them,
written because of this. `test_the_worker_records_health_as_it_goes` says so in
its docstring.

**What would have to change.** A coverage gate would catch the narrow case — an
uncalled method is uncovered — but only if coverage is enforced, and this
project deliberately does not gate on a percentage. Until something mechanical
exists, the discipline is: after adding a capability, grep for its own name and
count the callers.
