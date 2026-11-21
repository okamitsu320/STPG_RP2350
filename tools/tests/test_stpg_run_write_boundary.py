import pytest
from stpg.client import StpgClient,StpgClientError
from stpg.protocol import StpgStatusResponse
from stpg.constants import RSP_OK
from stpg.transport import SerialTransport

class Raw:
    def __init__(self,written=32,flush_error=None,response=None):self.written=written;self.flush_error=flush_error;self.response=response;self.events=[];self.timeout=1
    def write(self,data):self.events.append("write");return self.written
    def flush(self):
        self.events.append("flush")
        if self.flush_error:raise self.flush_error
    def read(self,n):self.events.append("read");return self.response or b""
    def close(self):pass

def transport(raw):
    t=object.__new__(SerialTransport);t._serial=raw;return t

def test_full_write_flush_callback_precedes_response_read():
    rsp=StpgStatusResponse(RSP_OK,1,1,0,0,0).pack();raw=Raw(response=rsp);t=transport(raw);events=[]
    result=StpgClient(t).run_with_write_boundary(lambda packet,seq:events.append(("intent",len(packet),seq)),lambda packet,seq:events.append(("callback",len(packet),seq)),run_flags=0x10100,aux_arg=0)
    assert result.seq==1 and raw.events[:2]==["write","flush"] and events==[("intent",32,1),("callback",32,1)] and raw.events[-1]=="read"

def test_short_write_and_missing_api_never_callback_or_read():
    raw=Raw(written=31);events=[]
    with pytest.raises(OSError,match="short RUN write"):StpgClient(transport(raw)).run_with_write_boundary(lambda *a:events.append("intent"),lambda *a:events.append("write"))
    assert events==["intent"] and "flush" not in raw.events and "read" not in raw.events
    class Legacy:
        def write(self,data):pass
        def read_exactly(self,size):raise AssertionError
    with pytest.raises(StpgClientError,match="lacks dedicated"):StpgClient(Legacy()).run_with_write_boundary(lambda *a:None,lambda *a:None)

def test_callback_failure_occurs_after_flush_before_read():
    raw=Raw();events=[]
    def fail(*_):events.append("callback");raise OSError("ledger fsync failed")
    with pytest.raises(OSError,match="ledger fsync"):StpgClient(transport(raw)).run_with_write_boundary(lambda *a:events.append("intent"),fail)
    assert raw.events==["write","flush"] and events==["intent","callback"]

def test_flush_failure_forbids_write_complete_callback_and_read():
    raw=Raw(flush_error=OSError("flush failed"));events=[]
    with pytest.raises(OSError,match="flush failed"):StpgClient(transport(raw)).run_with_write_boundary(lambda *a:events.append("intent"),lambda *a:events.append("host"))
    assert events==["intent"] and raw.events==["write","flush"]
