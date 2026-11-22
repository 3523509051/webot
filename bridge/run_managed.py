"""Managed bridge process; does not start the WeChat client."""
import argparse
import asyncio
import logging
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--stop-file',type=Path,required=True)
    parser.add_argument('--log',type=Path,required=True)
    args=parser.parse_args()
    args.log.parent.mkdir(parents=True,exist_ok=True)
    with args.log.open('a',encoding='utf-8',buffering=1) as stream:
        original=sys.stdout,sys.stderr;sys.stdout=sys.stderr=stream
        try:
            logging.basicConfig(stream=stream,level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
            import main as bridge
            async def run():
                async def watch():
                    while not args.stop_file.exists():await asyncio.sleep(0.25)
                task=asyncio.create_task(bridge.main());watcher=asyncio.create_task(watch())
                try:
                    done,_=await asyncio.wait([task,watcher],return_when=asyncio.FIRST_COMPLETED)
                    if task in done:return await task
                    task.cancel();await asyncio.gather(task,return_exceptions=True);return 0
                finally:
                    watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
            return asyncio.run(run())
        finally:sys.stdout,sys.stderr=original


if __name__=='__main__':raise SystemExit(main())
