"""Release-domain boundary marker.

Remote publication and merge effects are deliberately implemented by the
future deterministic Release Controller.  This module exists as the stable
domain seam for those operations; Task 3 keeps the contracts side-effect free
and does not persist or execute release actions.
"""

__all__: list[str] = []
