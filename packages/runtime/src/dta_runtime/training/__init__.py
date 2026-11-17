"""The frames models learn from, built beside the store.

`frame` is what a `FrameRef` names; `training_set` is the same with the label
of each row and what a reader has to know of the rows. Tables are Arrow and
the joins are DuckDB's: nothing here imports a model library.
"""

from .frames import MAX_CELLS, TrainingSet, frame, training_set
from .spec import TrainingError

__all__ = ["MAX_CELLS", "TrainingError", "TrainingSet", "frame", "training_set"]
