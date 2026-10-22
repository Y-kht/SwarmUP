# Runs one script wheel, in a process of its own (wheel_tools.py starts it with python -I, so nothing of SwarmUP is imported by chance):
#     python -I wheel_runner.py <wheel.py> <arguments.json> <result.json>
# It loads the wheel from its file, calls run(arguments, folder) in the folder it was started in, and writes what happened as JSON:
# {"ok": true, "output": text for the agent, "data": the value run gave, "printed": what the wheel printed}, or
# {"ok": false, "error": {"type", "message"}, "trace": the traceback, for the architect only, "printed"}.
# Only the standard library is used here.
import contextlib
import importlib.util
import io
import json
import os
import sys
import traceback


PRINTED_CHARS = 20000


def asOutput(value):
    if value is None:
        return "Done."
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=1, default=str)
    except (TypeError, ValueError):
        return str(value)


def runWheel(path, arguments):
    spec = importlib.util.spec_from_file_location("wheel", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise TypeError("The wheel has no run(args, folder) function.")
    return module.run(arguments, os.getcwd())


def main(argv):
    path, argumentsPath, resultPath = argv[1:4]
    with open(argumentsPath, encoding="utf-8") as handle:
        arguments = json.load(handle)
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            value = runWheel(path, arguments)
        try:
            json.dumps(value)
            data = value
        except (TypeError, ValueError):
            data = None
        result = {"ok": True, "output": asOutput(value), "data": data}
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt):
            raise
        result = {"ok": False, "error": {"type": type(error).__name__, "message": str(error)[:2000]}, "trace": traceback.format_exc()[-6000:]}
    result["printed"] = printed.getvalue()[-PRINTED_CHARS:]
    with open(resultPath, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, default=str)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
