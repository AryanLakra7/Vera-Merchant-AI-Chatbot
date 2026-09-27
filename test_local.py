import json, subprocess, time, sys, os
from urllib import request as R
U="http://localhost:8090"
def call(m,p,b=None):
    req=R.Request(U+p,data=json.dumps(b).encode() if b is not None else None,method=m,headers={"Content-Type":"application/json"})
    try: r=R.urlopen(req,timeout=10); return r.status,json.loads(r.read())
    except R.HTTPError as e: return e.code,json.loads(e.read())
env=dict(os.environ,PORT="8090",QUIET="1")
p=subprocess.Popen([sys.executable,"bot.py"],env=env); time.sleep(1)
try:
    d="dataset/"
    for f in os.listdir(d+"categories"):
        c=json.load(open(d+"categories/"+f)); print(call("POST","/v1/context",{"scope":"category","context_id":c["slug"],"version":1,"payload":c,"delivered_at":"x"})[0],end=" ")
    for m in json.load(open(d+"merchants_seed.json"))["merchants"]: call("POST","/v1/context",{"scope":"merchant","context_id":m["merchant_id"],"version":1,"payload":m,"delivered_at":"x"})
    for c in json.load(open(d+"customers_seed.json"))["customers"]: call("POST","/v1/context",{"scope":"customer","context_id":c["customer_id"],"version":1,"payload":c,"delivered_at":"x"})
    T=json.load(open(d+"triggers_seed.json"))["triggers"]
    for t in T: call("POST","/v1/context",{"scope":"trigger","context_id":t["id"],"version":1,"payload":t,"delivered_at":"x"})
    print("\nrepush:",call("POST","/v1/context",{"scope":"trigger","context_id":T[0]["id"],"version":1,"payload":T[0],"delivered_at":"x"}))
    print(call("GET","/v1/healthz"))
    allacts=[]
    for i in range(0,len(T),5):
        s,r=call("POST","/v1/tick",{"now":"2026-04-26T10:30:00Z","available_triggers":[t["id"] for t in T[i:i+5]]})
        allacts+=r["actions"]
    for a in allacts: print(f"\n[{a['trigger_id']}] ({a['send_as']}, {a['cta']})\n{a['body']}")
    print("\nTOTAL actions:",len(allacts),"of",len(T))
    cid=allacts[0]["conversation_id"]; mid=allacts[0]["merchant_id"]
    for msg in ["Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."]*4:
        print("AUTO ->",call("POST","/v1/reply",{"conversation_id":cid,"merchant_id":mid,"from_role":"merchant","message":msg,"received_at":"x","turn_number":2})[1])
    for msg in ["What would the patient message look like?","Ok lets do it. Whats next?","Btw can you also help me file GST?","Stop messaging me. This is useless spam."]:
        print(msg,"->",call("POST","/v1/reply",{"conversation_id":"c_x","merchant_id":"m_001_drmeera_dentist_delhi","from_role":"merchant","message":msg,"received_at":"x","turn_number":2})[1])
finally: p.terminate()
