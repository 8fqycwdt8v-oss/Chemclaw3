import asyncio, os, sys, tempfile
from datetime import UTC, datetime
from pathlib import Path
tmp = Path(tempfile.mkdtemp()); os.environ["MOCK_ELN_EXPORT_DIR"]=str(tmp/"json"); os.environ["MOCK_ORD_EXPORT_DIR"]=str(tmp/"ord")
sys.path.insert(0, os.environ["CHEMCLAW_MOCK_REPO"])
from app.eln.seed import seed_all; seed_all()
from chemclaw.ingest.eln.ord_adapter import OrdJsonAdapter
from collections import Counter
a=OrdJsonAdapter(str(tmp/"ord")); c=Counter()
for raw in asyncio.run(a.fetch_new_entries(datetime(1970,1,1,tzinfo=UTC))):
    try: a.map_to_ord(raw)
    except Exception as e: c[str(e)[:160].split(":",1)[-1]]+=1
print(c.most_common(3))
