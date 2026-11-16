"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs/promises");
const http=require("node:http");
const path=require("node:path");
const {spawn}=require("node:child_process");
const {devices,modes,rtt,playerJob}=require("./fixture.cjs");
const root=process.env.PR112_BIGSCREEN;
const output=path.join(__dirname,"evidence");
const head="690268a7401efdadcce97258e51c883505ecf539";
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const pct=(items,p=.95)=>{const a=[...items].sort((x,y)=>x-y);return a.length?Math.round(a[Math.ceil(a.length*p)-1]*100)/100:null};
let fixture=devices(50), calls=[], active=0, peak=0, missingVip=false, unitRestored=false, toggleCore=false, extraDelay=false, timeoutStatus=false, timeoutTrend=false;
function vector(item,at){
  const t=Math.floor(at/item.interval)*item.interval;
  const mode=missingVip?"up":toggleCore&&item.i===0?"down":unitRestored&&item.i===2?"up":modes(item,null);
  const sourceAt=mode==="missing"?t-70:t;
  const count=(item.metric.job==="infra-ap-ping"?300:60)/item.interval;
  const fields={target:1,source:mode==="scrape"?0:1,source_at:sourceAt,probe:mode==="down"?0:1,
    probe_at:t,rtt:Number(rtt(item,t)),rtt_at:t,count,ok:mode==="down"?0:count};
  return Object.entries(fields).map(([field,value])=>({metric:{...item.metric,ping_field:field},value:[at,String(value)]}));
}
function matrix(item,end,span) {
  const series={up:[],probe_success:[],probe_icmp_duration_seconds:[]};
  for(let t=Math.floor((end-span)/item.interval)*item.interval+item.interval;t<=end;t+=item.interval){
    const mode=modes(item,t);
    if(mode==="missing")continue;
    series.up.push([t,mode==="scrape"?"0":"1"]);
    if(mode==="scrape")continue;
    series.probe_success.push([t,mode==="down"?"0":"1"]);
    if(mode==="up")series.probe_icmp_duration_seconds.push([t,rtt(item,t)]);
  }
  return Object.entries(series).filter(([,v])=>v.length).map(([name,values])=>({metric:{...item.metric,__name__:name,...(name==="probe_icmp_duration_seconds"?{phase:"rtt"}:{})},values}));
}
function selected(expr) {
  const m=expr.match(/instance=(~?)("(?:\\.|[^"\\])*")/);
  if(!m)return ()=>true;
  const label=JSON.parse(m[2]);const re=m[1]?new RegExp("^(?:"+label+")$"):null;
  return name=>re?re.test(name):name===label;
}
const server=http.createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,"http://localhost");
    if(url.pathname.startsWith("/prometheus/api/v1/")){
      const start=performance.now(), expr=url.searchParams.get("query")||"";
      const time=Number(url.searchParams.get("time")||url.searchParams.get("end"));
      const raw=expr.includes('__name__=~"up|probe_success|probe_icmp_duration_seconds"');
      const summary=expr.includes('"target"')&&expr.includes('"source_at"');
      const span=Number(expr.match(/\[(\d+)s\]$/)?.[1])||0;
      const testName=selected(expr);
      const targets=fixture.filter(d=>testName(d.metric.instance)&&d.metric.job!==playerJob);
      const track={kind:summary?"summary":raw&&span===300?"trend":"history",start,time,targets:raw?targets.map(d=>d.metric.instance):undefined};
      calls.push(track);if(track.kind==="trend"){active++;peak=Math.max(peak,active);}
      res.once("close",()=>{track.ms=Math.round((performance.now()-start)*100)/100;track.aborted=!res.writableFinished;if(track.kind==="trend")active--;});
      if(summary&&timeoutStatus||raw&&timeoutTrend){res.writeHead(503,{"content-type":"application/json"}).end(JSON.stringify({status:"error",errorType:"timeout"}));return;}
      if(raw&&extraDelay)await sleep(1250);
      if(res.destroyed)return;
      const resultType=raw||url.pathname.endsWith("query_range")?"matrix":"vector";
      // Inject an out-of-scope player row even in the summary to exercise the client's guard.
      const result=summary?[...fixture.filter(x=>!missingVip||x.i!==1).flatMap(x=>vector(x,time)),
        {metric:{job:playerJob,instance:"Retired-Player-Sentinel",ping_field:"aged_out"},value:[time,"1"]}]:
        raw?targets.flatMap(x=>matrix(x,time,span)):[];
      res.writeHead(200,{"content-type":"application/json","cache-control":"no-store"}).end(JSON.stringify({status:"success",data:{resultType,result}}));
      return;
    }
    const file=url.pathname==="/ping"?"index.html":url.pathname.slice(1), abs=path.resolve(root,file);
    const types={".html":"text/html; charset=utf-8",".js":"text/javascript; charset=utf-8",".css":"text/css; charset=utf-8",".json":"application/json"};
    if(!abs.startsWith(root+path.sep)||!types[path.extname(abs)]){res.writeHead(404).end();return;}
    let body=await fs.readFile(abs);
    if(file==="config.js")body=Buffer.concat([body,Buffer.from('\nwindow.BIGSCREEN_CONFIG.firewallPing = "VIP:192.0.2.2";')]);
    res.writeHead(200,{"content-type":types[path.extname(abs)],"cache-control":"no-store"}).end(body);
  }catch(e){if(!res.headersSent)res.writeHead(404);res.end();}
});
async function main(){
  await fs.mkdir(output,{recursive:true});
  await new Promise((r,j)=>server.listen(0,"127.0.0.1",r).on("error",j));
  const base="http://127.0.0.1:"+server.address().port;
  const chrome=spawn(process.env.PR112_CHROME,["--headless=new","--no-sandbox","--disable-gpu","--no-first-run","--no-default-browser-check","--disable-background-networking","--disable-extensions","--remote-debugging-pipe","--user-data-dir="+path.join(__dirname,"chrome-profile"),"about:blank"],{windowsHide:true,stdio:["ignore","ignore","ignore","pipe","pipe"]});
  let sequence=0, sessionId, input="";
  const pending=new Map(), exceptions=[];
  chrome.stdio[4].setEncoding("utf8").on("data",chunk=>{
    input+=chunk;let at;
    while((at=input.indexOf("\0"))>=0){
      const event=JSON.parse(input.slice(0,at));input=input.slice(at+1);
      if(event.method==="Runtime.exceptionThrown")exceptions.push(event.params.exceptionDetails.exception?.description||event.params.exceptionDetails.text);
      const done=pending.get(event.id);if(!done)continue;
      pending.delete(event.id);clearTimeout(done.timeout);
      if(event.error)done.reject(new Error(event.error.message));else done.resolve(event.result);
    }
  });
  const send=(method,params={},browser=false)=>new Promise((resolve,reject)=>{
    const id=++sequence, timeout=setTimeout(()=>{pending.delete(id);reject(Error("CDP "+method+" timeout"));},13000);
    pending.set(id,{resolve,reject,timeout});chrome.stdio[3].write(JSON.stringify({id,method,params,sessionId:browser?undefined:sessionId})+"\0");
  });
  const evaluate=async expression=>{
    const r=await send("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});
    if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);
    return r.result.value;
  };
  const wait=async (condition,limit=8000)=>{
    const start=performance.now();
    while(performance.now()-start<limit){if(await evaluate(condition))return performance.now()-start;await sleep(40);}
    throw Error("Condition failed: "+condition+" exceptions="+exceptions.join("|"));
  };
  const runtime=async()=>Object.fromEntries((await send("Performance.getMetrics")).metrics.map(({name,value})=>[name,value]));
  const processInfo=async()=> (await send("SystemInfo.getProcessInfo",{},true)).processInfo;
  async function chromeRss(processes){
    const reads=await Promise.all(processes.map(proc=>fs.readFile("/proc/"+proc.id+"/status","utf8").catch(()=>null)));
    return Math.round(reads.reduce((s,t)=>s+Number(t?.match(/^VmRSS:\s+(\d+)/m)?.[1]||0),0)/1024*100)/100;
  }
  const screen=(width,height)=>send("Emulation.setDeviceMetricsOverride",{width,height,deviceScaleFactor:1,mobile:false});
  const navigate=(query)=>send("Page.navigate",{url:base+"/ping"+query});
  const cardCount='document.querySelectorAll(".ping-card").length';
  const spark='document.querySelectorAll(".ping-card svg").length';
  const filter=(id,value)=>evaluate('(()=>{const f=document.querySelector('+JSON.stringify(id)+');f.value='+JSON.stringify(value)+';f.dispatchEvent(new Event("input"));return '+cardCount+'})()');
  const state=(index)=>evaluate('document.querySelectorAll(".ping-card")['+index+']?.dataset.state');
  const order=()=>evaluate('[...document.querySelectorAll(".ping-card strong")].map(e=>e.textContent)');
  const click=(s)=>evaluate('document.querySelector('+JSON.stringify(s)+').click()');
  const checks=[];
  try{
    const {targetInfos}=await send("Target.getTargets",{},true);
    ({sessionId}=await send("Target.attachToTarget",{targetId:targetInfos.find(x=>x.type==="page").targetId,flatten:true},true));
    await send("Runtime.enable");await send("Page.enable");await send("Performance.enable");
    const version=await send("Browser.getVersion",{},true);
    await send("Browser.setDownloadBehavior",{behavior:"allow",downloadPath:output},true);
    await screen(1920,1080);
    fixture=devices(100);
    await navigate("?acceptance=112");
    await wait(cardCount+'===100 && '+spark+'>20');
    assert.deepEqual((await order()).slice(0,7),["Core A","FW VIP A","FW Unit A","FW Unit B","ISP A","ISP B","ISP C"]);
    assert.deepEqual(await Promise.all([state(0),state(1),state(2),state(4),state(5),state(6)]),["UP","UP","DOWN","DEGRADED","UNKNOWN","UNKNOWN"]);
    assert.match(await evaluate('document.querySelectorAll(".ping-card-note")[5].textContent'),/抓取失败/);
    assert.match(await evaluate('document.querySelectorAll(".ping-card-note")[6].textContent'),/过期|缺失/);
    assert.deepEqual(await evaluate('["missing","loss","scrape"].map(x=>!!document.querySelector(".ping-card .ping-mark-"+x))'),[true,true,true]);
    checks.push("Core → FW VIP → FW Unit → ISP; VIP UP / member DOWN; four states, labels and trend marks");
    for(const [width,height,columns] of [[1920,1080,5],[1366,768,4],[390,844,1]]){
      await screen(width,height);await evaluate('document.querySelector("#pingWorkspace").scrollTop=0');
      await wait(spark+'>0');await sleep(250);
      const layout=await evaluate('(()=>{const r=document.querySelector("#pingWorkspace");return [getComputedStyle(document.querySelector("#pingWall")).gridTemplateColumns.split(" ").length,r.scrollWidth,r.clientWidth,document.documentElement.scrollWidth,innerWidth]})()');
      assert.equal(layout[0],columns);assert.ok(layout[1]<=layout[2]&&layout[3]<=layout[4],String(layout));
      const shot=await send("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});
      await fs.writeFile(path.join(output,"pr112-690268a-"+width+"x"+height+".png"),Buffer.from(shot.data,"base64"));
      checks.push(width+"x"+height+" "+columns+" columns no overflow");
    }
    await screen(1920,1080);await wait(spark+'>15');
    missingVip=true;unitRestored=true;
    await wait('document.querySelector("#pingCoverage").textContent.includes("已配置 FW VIP 缺少")');
    assert.equal(await evaluate('document.querySelector("#pingSource").dataset.state'),"DEGRADED");
    assert.deepEqual((await order()).slice(0,3),["Core A","FW Unit A","FW Unit B"]);
    assert.equal(await state(1),"UP");assert.equal(await state(2),"UP");
    assert.match(await evaluate('document.querySelector("#pingCounts").textContent'),/未知 0(?:\D|$)/);
    checks.push("missing configured VIP still partial with both HA members UP");
    missingVip=false;unitRestored=false;
    await wait(cardCount+'===100');
    const keys=await evaluate('[...document.querySelectorAll(".ping-card")].map(e=>e.dataset.key)');
    await evaluate('window.auditCard=document.querySelector(".ping-card");auditCard.querySelector(".ping-card-open").focus()');
    toggleCore=true;await wait('auditCard.dataset.state==="DOWN"');
    assert.equal(await evaluate('document.activeElement === auditCard.querySelector(".ping-card-open")'),true);
    assert.deepEqual(await evaluate('[...document.querySelectorAll(".ping-card")].map(e=>e.dataset.key)'),keys);
    toggleCore=false;
    assert.equal(await filter("#pingSearch","Player-Sentinel"),0);
    assert.equal(await filter("#pingSearch",""),100);
    assert.equal(await filter("#pingCategory","ap"),fixture.filter(e=>e.metric.job==="infra-ap-ping").length);
    assert.ok((await order()).every(x=>x.startsWith("AP ")));assert.equal(await filter("#pingCategory",""),100);
    assert.ok(await filter("#pingStatus","abnormal")>0);
    assert.equal(await evaluate('document.querySelectorAll(".ping-card[data-state=UP]").length'),0);
    assert.equal(await filter("#pingStatus",""),100);
    const total=await evaluate('document.querySelector("#pingCounts").textContent.match(/\\d+/g).map(Number).reduce((a,b)=>a+b,0)');
    assert.equal(total,100);
    assert.equal(await evaluate('document.querySelector("#pingCoverage").textContent'),"");
    checks.push("AP remains, player excluded from list/search/count; state refresh preserves identity, order and focus; filters work");
    await evaluate('[...document.querySelectorAll(".ping-card-compare")].slice(0,4).forEach(e=>e.click())');
    assert.equal(await evaluate('document.querySelectorAll("#pingSelected button").length'),4);
    assert.equal(await evaluate('document.querySelectorAll(".ping-card-compare")[4].disabled'),true);
    await click("#pingComparison");
    await wait('["P95","STDDEV"].every(x=>document.querySelector("#pingMetrics").textContent.includes(x))&&!document.querySelector("#pingExport").disabled');
    const coreStats=await evaluate('[...document.querySelectorAll(".ping-metrics-card")].find(card=>card.textContent.includes("Core A")) && Object.fromEntries([...document.querySelectorAll(".ping-metrics-card")].find(card=>card.textContent.includes("Core A")).querySelectorAll("dt").values().map(dt=>[dt.textContent.trim(),Number.parseFloat(dt.nextElementSibling.textContent)]))');
    assert.ok(coreStats?.P95>0&&coreStats?.STDDEV>0,"Core A P95 and STDDEV should both be numeric");
    await click("#pingExport");
    let csv;
    for(let i=0;i<25&&!csv;i++){csv=(await fs.readdir(output)).find(f=>/^ping.*\.csv$/.test(f));if(!csv)await sleep(80);}
    assert.ok(csv,"CSV file");
    const text=await fs.readFile(path.join(output,csv),"utf8");
    assert.match(text,/timestamp_utc,job,name,ip,target_labels,scrape_up,probe_success,rtt_ms/);
    for(const name of ["Core A","FW VIP A","FW Unit A","FW Unit B"])assert.ok(text.includes(name));
    assert.ok(!text.includes("Player-Sentinel"));
    await click("#pingBackToWall");assert.equal(await evaluate('document.activeElement.id'),"pingComparison");
    const member=await evaluate('document.querySelectorAll(".ping-card")[2].dataset.key');
    await navigate("?target="+encodeURIComponent(member));
    await wait('document.querySelectorAll("#pingSelected button").length===1&&document.querySelector("#pingSelected").textContent.includes("FW Unit A")&&!document.querySelector("#pingExport").disabled');
    assert.equal(await evaluate('document.activeElement.id'),"pingHistoryTitle");
    await screen(390,844);
    assert.equal(await evaluate('document.querySelector("#pingWorkspace").scrollWidth <= document.querySelector("#pingWorkspace").clientWidth'),true);
    checks.push("four-way compare limit, history P95/STDDEV, downloadable CSV; exact identity deep link and mobile history");
    await screen(1920,1080);
    timeoutStatus=true;await wait('document.querySelector("#pingSource").textContent.includes("查询超时")');timeoutStatus=false;
    await wait('!document.querySelector("#pingSource").textContent.includes("查询超时")');
    timeoutTrend=true;await navigate("?timeout=trend");
    await wait('location.search==="?timeout=trend"&&[...document.querySelectorAll(".ping-card-note")].some(e=>e.textContent==="查询超时")');timeoutTrend=false;
    await navigate("?recover=trend");await wait('location.search==="?recover=trend"&&'+spark+'>15');
    extraDelay=true;const previous=calls.length;
    await navigate("?cancel=filter");await wait('location.search==="?cancel=filter"&&'+cardCount+'===100');await sleep(210);
    await filter("#pingSearch","no-such-target");await sleep(240);
    const aborted=calls.slice(previous).filter(x=>x.aborted).length;assert.ok(aborted>=1);
    assert.equal(await evaluate(spark),0);
    await filter("#pingSearch","");await sleep(240);
    const leaving=calls.length;await send("Page.navigate",{url:"about:blank"});await sleep(180);
    assert.ok(calls.slice(previous,leaving).some(x=>x.aborted));
    extraDelay=false;
    checks.push("status and trend timeout text, recovery, cancellation after filter and leaving without stale SVG");
    const bench=[];
    for(const n of [50,100,300])for(let round=1;round<=3;round++){
      fixture=devices(n);calls=[];peak=active;
      const hash="?n="+n+"&round="+round, start=performance.now();
      await navigate(hash);await wait("location.search==="+JSON.stringify(hash)+" && "+cardCount+"==="+n);
      const ready=performance.now();await wait(spark+'>=20');
      const loaded=performance.now(), before=await runtime(), procBefore=await processInfo();
      const frames=await evaluate('(async()=>{const root=document.querySelector("#pingWorkspace");const bottom=root.scrollHeight-root.clientHeight;let p=performance.now();const out=[];for(const [from,to] of [[0,.6],[.6,1],[1,0]]){const start=performance.now();let t;do{t=await new Promise(requestAnimationFrame);out.push(t-p);p=t;root.scrollTop=bottom*(from+(to-from)*Math.min(1,(t-start)/1700));}while(t-start<1700);const hold=performance.now();do{t=await new Promise(requestAnimationFrame);out.push(t-p);p=t;}while(t-hold<300);}return out.slice(1)})()');
      await wait(spark+'>=20');const after=await runtime(), procAfter=await processInfo();
      const procDelta=procAfter.reduce((s,p)=>s+Math.max(0,p.cpuTime-(procBefore.find(a=>a.id===p.id)?.cpuTime??p.cpuTime)),0);
      const requests=calls.filter(e=>e.kind==="trend"), cold=requests.filter(e=>e.start<loaded);
      // Require at most one successful request per target per aligned trend window.
      const seen=new Set();let duplicates=0;
      for(const row of requests.filter(x=>!x.aborted&&x.ms!=null))for(const name of row.targets){
        const key=row.time+":"+name;if(seen.has(key))duplicates++;seen.add(key);
      }
      assert.equal(duplicates,0);assert.ok(peak<=2,"trend concurrency "+peak);assert.ok(requests.every(r=>r.targets.length<=12));
      assert.equal(await evaluate(cardCount),n);
      const samples={n,round,chromeRssMiB:await chromeRss(procAfter),coldMs:Math.round(loaded-start),screenMs:Math.round(loaded-ready),coldTrend:cold.length,statusRequests:calls.filter(e=>e.kind==="summary").length,trend:requests.length,peak,aborts:requests.filter(e=>e.aborted).length,duplicates,frames:frames.length,frameP95:pct(frames),frameMax:pct(frames,1),jsMiB:Math.round(after.JSHeapUsedSize/1048576*100)/100,mainThreadPct:Math.round(10000*(after.TaskDuration-before.TaskDuration)/(after.Timestamp-before.Timestamp))/100,chromeCpuPct:Math.round(10000*procDelta/(after.Timestamp-before.Timestamp))/100};
      bench.push(samples);
      await fs.writeFile(path.join(__dirname,"browser-progress.json"),JSON.stringify({checks,bench},null,2));
    }
    assert.deepEqual(exceptions,[]);
    const report={head,version:version.product,checks,bench,exceptions,backEnd:"synthetic HTTP",clients:1,notes:["CPU is CDP process CPU time delta as percent of one logical core; heap is JS only.", "First screen points >=20; explicit scroll pauses to test new viewport loads.","Chromium RSS sums the browser process-tree RSS and may count shared pages more than once."]};
    await fs.writeFile(path.join(output,"pr112-browser-results.json"),JSON.stringify(report,null,2));
    return report;
  } finally {try{await send("Browser.close",{},true)}catch{chrome.kill();} server.closeAllConnections();server.close();}
}
main().then(r=>console.log(JSON.stringify({status:"PASS",checks:r.checks.length,runs:r.bench.length}))).catch(e=>{console.error(e.stack);server.closeAllConnections();server.close();process.exitCode=1;});