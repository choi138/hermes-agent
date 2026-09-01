"""Reproduce the rvw finding: BlueBubbles adapter + path B -> TypeError.

Also checks yuanbao (the other override) for the same shape.
"""
import sys
import traceback

sys.path.insert(0, ".")


def probe(name, module_path, cls_name):
    import importlib
    try:
        mod = importlib.import_module(module_path)
    except Exception as e:
        print(f"[{name}] import failed: {type(e).__name__}: {e}")
        return
    cls = getattr(mod, cls_name, None)
    if cls is None:
        print(f"[{name}] class {cls_name} not found")
        return
    fn = getattr(cls, "truncate_message", None)
    import inspect
    sig = inspect.signature(fn)
    accepts = "len_fn" in sig.parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    print(f"[{name}] {cls_name}.truncate_message{sig}")
    print(f"        accepts len_fn: {accepts}")

    # Direct call exactly as _truncate_for_stream does for BasePlatformAdapter
    try:
        out = fn("hello world\n" * 50, 100, len_fn=len)
        print(f"        call with len_fn= -> ok, {len(out)} chunks")
    except TypeError as e:
        print(f"        call with len_fn= -> TypeError: {e}   <<< CONFIRMED")
    except Exception as e:
        print(f"        call with len_fn= -> {type(e).__name__}: {e}")
    print()


print("=== base ===")
probe("base", "gateway.platforms.base", "BasePlatformAdapter")
print("=== bluebubbles ===")
probe("bluebubbles", "gateway.platforms.bluebubbles", "BlueBubblesAdapter")
print("=== yuanbao ===")
probe("yuanbao", "gateway.platforms.yuanbao", "YuanbaoAdapter")
