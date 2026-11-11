import itertools
import random

import pytest
from dta_toolkit.tasks.batches import conflicts, plan_batches
from dta_toolkit.tasks.catalogue import TaskSpec
from dta_toolkit.tasks.errors import TaskError


def _task(task_id, reads=(), owns=(), needs=()):
    return TaskSpec(id=task_id, kind="synthetic", stage="x", reads=reads, owns=owns, needs=needs)


def test_disjoint_tasks_share_one_batch():
    tasks = [
        _task("a", reads=["ontology", "sources.a"], owns=["sources.a.profile"]),
        _task("b", reads=["ontology", "sources.b"], owns=["sources.b.profile"]),
        _task("c", reads=["ontology", "contract.questions"]),  # shared reads never conflict
        _task("d", reads=["ontology", "contract.questions"], owns=["models.d"]),
    ]
    assert plan_batches(tasks) == [["a", "b", "c", "d"]]


def test_a_task_does_not_conflict_with_itself():
    # It reads the very subtree it writes into.
    task = _task("a", reads=["sources.a"], owns=["sources.a.profile"])
    assert plan_batches([task]) == [["a"]]


def test_two_writers_of_one_key_are_split_in_id_order():
    tasks = [
        _task("t2", owns=["models.m1.candidates"]),
        _task("t1", owns=["models.m1.candidates"]),
        _task("t3", owns=["models.m2.candidates"]),
    ]
    assert plan_batches(tasks) == [["t1", "t3"], ["t2"]]


def test_a_writer_is_split_from_a_reader_of_the_same_key():
    writer = _task("w", owns=["sources.a.mappings"])
    reader = _task("r", reads=["sources.a"])
    bystander = _task("x", reads=["sources.b"])
    assert conflicts(writer, reader) and conflicts(reader, writer)
    assert not conflicts(writer, bystander)
    assert plan_batches([writer, reader, bystander]) == [["r", "x"], ["w"]]


def test_writing_a_parent_conflicts_with_writing_a_child():
    parent = _task("p", owns=["models.m1"])
    child = _task("c", owns=["models.m1.features"])
    assert conflicts(parent, child)
    assert plan_batches([parent, child]) == [["c"], ["p"]]


def test_a_shared_prefix_of_characters_is_not_an_overlap():
    tasks = [
        _task("one", reads=["sources.a"], owns=["sources.a.profile"]),
        _task("two", reads=["sources.ab"], owns=["sources.ab.profile"]),
    ]
    assert not conflicts(*tasks)
    assert plan_batches(tasks) == [["one", "two"]]


def test_patterns_conflict_with_the_keys_they_match():
    wide = _task("wide", owns=["functions.q1-*"])
    inside = _task("inside", owns=["functions.q1-kpi"])
    outside = _task("outside", owns=["functions.q1", "functions.q10"])
    nested = _task("nested", owns=["functions.q1-a-*"])
    assert conflicts(wide, inside) and conflicts(wide, nested)
    assert not conflicts(wide, outside)
    assert plan_batches([wide, inside, outside, nested]) == [
        ["inside", "nested", "outside"],
        ["wide"],
    ]


def test_needs_come_first_and_conflicts_split_inside_a_layer():
    tasks = [
        _task("build-a", needs=["prepare-a"]),
        _task("build-b", needs=["prepare-b"], owns=["models.shared"]),
        _task("build-c", needs=["prepare-a"], owns=["models.shared"]),
        _task("prepare-a"),
        _task("prepare-b"),
        _task("ship", needs=["build-a", "build-b"]),
    ]
    assert plan_batches(tasks) == [
        ["prepare-a", "prepare-b"],
        ["build-a", "build-b"],
        ["build-c"],
        ["ship"],
    ]


def test_batches_do_not_depend_on_the_order_tasks_are_given_in():
    tasks = [
        _task("a", owns=["k.one"]),
        _task("b", owns=["k.one"], reads=["k.two"]),
        _task("c", owns=["k.two"]),
        _task("d", reads=["k.one"], needs=["a"]),
        _task("e", owns=["k.three"], needs=["c"]),
    ]
    expected = plan_batches(tasks)
    for shuffled in itertools.permutations(tasks):
        assert plan_batches(list(shuffled)) == expected


def test_every_batch_of_a_random_task_set_is_safe():
    rng = random.Random(20261110)
    keys = [f"zone.k{i}" for i in range(6)] + [f"zone.k{i}.sub" for i in range(6)] + ["zone.k1-*"]
    for _ in range(300):
        tasks = []
        for index in range(rng.randint(1, 9)):
            earlier = [task.id for task in tasks]
            tasks.append(
                _task(
                    f"t{index}",
                    reads=rng.sample(keys[:12], rng.randint(0, 3)),
                    owns=rng.sample(keys, rng.randint(0, 2)),
                    needs=rng.sample(earlier, min(len(earlier), rng.randint(0, 2))),
                )
            )
        by_id = {task.id: task for task in tasks}
        batches = plan_batches(tasks)
        assert sorted(itertools.chain.from_iterable(batches)) == sorted(by_id)
        position = {task_id: index for index, batch in enumerate(batches) for task_id in batch}
        for batch in batches:
            assert batch == sorted(batch)
            for first, second in itertools.combinations(batch, 2):
                assert not conflicts(by_id[first], by_id[second])
        for task in tasks:
            assert all(position[need] < position[task.id] for need in task.needs)


def test_a_cycle_of_needs_is_refused():
    tasks = [_task("a", needs=["b"]), _task("b", needs=["c"]), _task("c", needs=["a"])]
    with pytest.raises(TaskError) as caught:
        plan_batches(tasks)
    assert caught.value.code == "tasks.plan.cycle"
    with pytest.raises(TaskError):
        plan_batches([_task("self", needs=["self"])])


def test_an_unknown_need_and_a_repeated_id_are_refused():
    with pytest.raises(TaskError) as caught:
        plan_batches([_task("a", needs=["ghost"])])
    assert caught.value.code == "tasks.plan.unknown_need"
    with pytest.raises(TaskError) as caught:
        plan_batches([_task("a"), _task("a")])
    assert caught.value.code == "tasks.plan.duplicate"


def test_a_task_definition_rejects_unsafe_ids_and_paths():
    for bad in ["../x", "a/b", "A", "", "a.b", "-a", "a b"]:
        with pytest.raises(ValueError):
            _task(bad)
    with pytest.raises(ValueError):
        _task("a", reads=["sources.*"])  # reads are plain paths
    with pytest.raises(TaskError):
        _task("a", owns=["sources.a*b"])
