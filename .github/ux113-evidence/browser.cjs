"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs/promises");
const http=require("node:http");
const path=require("node:path");
const {spawn}=require("node:child_process");
const {devices,modes,rtt,playerJob}=require("./fixture.cjs");
const root=process.env.PR112_BIGSCREEN;
const output=path.join(__dirname,"evidence");
const head="73b75a296c2cade44a3374c372c642ac69c39dc7";
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
    const spike = item.i === 0 && t === Math.floor(end / 2) * 2 - 34;
    const mode=spike || (item.i===0 && t===Math.floor(end/2)*2-36) ? "up" : modes(item,t);
    if(mode==="missing")continue;
    series.up.push([t,mode==="scrape"?"0":"1"]);
    if(mode==="scrape")continue;
    series.probe_success.push([t,mode==="down"?"0":"1"]);
    if(mode==="up")series.probe_icmp_duration_seconds.push([t,spike?"0.128237":rtt(item,t)]);
  }
  return Object.entries(series).filter(([,v])=>v.length).map(([name,values])=>({metric:{...item.metric,__name__:name,...(name==="probe_icmp_duration_seconds"?{phase:"rtt"}:{})},values}));
}
function selected(expr) {
  const m=expr.match(/instance=(~?)("(?:\\.|[^"\\])*")/);
  if(!m)return ()=>true;
  const label=JSON.parse(m[2]);const re=m[1]?new RegExp("^(?:"+label+")$"):null;
  return name=>re?re.test(name):name===label;
}
function intervalFields(target, expr, start, end, step, historical) {
  return [...expr.matchAll(/"ping_field", "([a-z_]+)"/g)].map(([,field]) => {
    const values=[];
    for(let t=start;t<=end;t+=step){
      const at=Math.floor(t/target.interval)*target.interval;
      const count=Math.floor(step/target.interval);
      const fields={source:1,source_at:at,probe:1,probe_at:at,rtt:rtt(target,at),rtt_at:at,count,ok:count};
      values.push([t,String(fields[field])]);
    }
    const metric={...target.metric,ping_field:field};
    return historical ? {metric,values} : {metric,value:values[0]};
  });
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
      const track={kind:summary?"summary":raw&&span===300?"trend":"history",start,time,span,raw,ranged:url.pathname.endsWith("query_range"),targets:raw?targets.map(d=>d.metric.instance):undefined};
      calls.push(track);if(track.kind==="trend"){active++;peak=Math.max(peak,active);}
      res.once("close",()=>{track.ms=Math.round((performance.now()-start)*100)/100;track.aborted=!res.writableFinished;if(track.kind==="trend")active--;});
      if(summary&&timeoutStatus||raw&&timeoutTrend){res.writeHead(503,{"content-type":"application/json"}).end(JSON.stringify({status:"error",errorType:"timeout"}));return;}
      if(raw&&extraDelay)await sleep(1250);
      if(res.destroyed)return;
      const resultType=raw||url.pathname.endsWith("query_range")?"matrix":"vector";
      // Inject an out-of-scope player row even in the summary to exercise the client's guard.
      const result=summary?[...fixture.filter(x=>!missingVip||x.i!==1).flatMap(x=>vector(x,time)),
        {metric:{job:playerJob,instance:"Retired-Player-Sentinel",ping_field:"aged_out"},value:[time,"1"]}]:
        raw?targets.flatMap(x=>matrix(x,time,span)):targets.flatMap(x=>{
          const step=Number(url.searchParams.get("step"))||Number(expr.match(/\[(\d+)s\]/)?.[1])||1;
          return intervalFields(x,expr,Number(url.searchParams.get("start")||time),time,step,track.ranged);
        });
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
    const overview="#pingOverviewChart svg", drill="#pingDistribution svg";
    const exists=(s)=>'!!document.querySelector('+JSON.stringify(s)+')';
    const span=(s=overview)=>evaluate('(()=>{const d=document.querySelector('+JSON.stringify(s)+').dataset;return {start:+d.chartStart,end:+d.chartEnd}})()');
    const pos=(s,t)=>evaluate('(()=>{const g=document.querySelector('+JSON.stringify(s)+');const p=g.createSVGPoint();p.x=53+829*('+t+'-Number(g.dataset.chartStart))/(g.dataset.chartEnd-g.dataset.chartStart);p.y=68;const o=p.matrixTransform(g.getScreenCTM());return {x:o.x,y:o.y};})()');
    const mouse=(type,p,more={})=>send("Input.dispatchMouseEvent",{type,...p,...more});
    const pointerClick=async(p)=>{await mouse("mouseMoved",p);await mouse("mousePressed",p,{button:"left",buttons:1,clickCount:1});await mouse("mouseReleased",p,{button:"left",buttons:0,clickCount:1});};
    const scroll=(s)=>evaluate('document.querySelector('+JSON.stringify(s)+').scrollIntoView({block:"center"})');
    const historyOn='document.querySelector("#pingDetail")?.hidden===false&&document.querySelector("#pingWallView")?.hidden===true';
    const historyOff='document.querySelector("#pingDetail")?.hidden===true&&document.querySelector("#pingWallView")?.hidden===false';
    const spike=(range)=>Math.floor(range.end/2)*2-34;
    const pinned='document.querySelector("#pingPinned").textContent';
    const plotKey=(type,key)=>send("Input.dispatchKeyEvent",{type,key,code:key});
    const shot=async(name)=>{
      const bytes=await send("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});
      await fs.writeFile(path.join(output,name+".png"),Buffer.from(bytes.data,"base64"));
    };
    const layout=async (w,h,columns)=>{
      await screen(w,h);
      const got=await evaluate('(()=>{const r=document.querySelector("#pingWorkspace");return [getComputedStyle(document.querySelector("#pingWall")).gridTemplateColumns.split(" ").length,r.scrollWidth,r.clientWidth,document.documentElement.scrollWidth,innerWidth]})()');
      assert.equal(got[0],columns);assert.ok(got[1]<=got[2]&&got[3]<=got[4],String(got));
    };

    fixture=devices(100);
    await screen(1920,1080);
    await navigate("?acceptance=113");
    await wait(historyOff+'&&'+cardCount+'===100&&'+spark+'>12');
    assert.deepEqual((await order()).slice(0,7),["Core A","FW VIP A","FW Unit A","FW Unit B","ISP A","ISP B","ISP C"]);
    assert.deepEqual(await Promise.all([state(1),state(2),state(4),state(5)]),["UP","DOWN","DEGRADED","UNKNOWN"]);
    assert.equal(await filter("#pingSearch","Player-Sentinel"),0);
    assert.equal(await filter("#pingSearch",""),100);
    assert.equal(await filter("#pingCategory","ap"),fixture.filter(x=>x.metric.job==="infra-ap-ping").length);
    assert.equal(await filter("#pingCategory",""),100);
    for(const [w,h,c] of [[390,844,1],[1366,768,4],[1920,1080,5]])await layout(w,h,c);
    await wait(spark+">12");
    assert.match(await evaluate('document.querySelector(".ping-card-compare").textContent'),/\+ 对比/);
    await click(".ping-card-compare");
    assert.match(await evaluate('document.querySelector(".ping-card-compare").textContent'),/已选/);
    assert.equal(await evaluate(historyOff),true);
    await click(".ping-card-compare");
    checks.push("wall 5/4/1, no overflow, group priority, states, AP, player exclusion, visible compare labels");

    assert.equal(await filter("#pingSearch","Core"),1);
    await evaluate('document.querySelector(".ping-card-open").focus(); document.querySelector(".ping-card-open").click();');
    await wait(historyOn+"&&"+exists(overview));
    assert.match(await evaluate('location.search'),/view=history/);
    assert.match(await evaluate('document.querySelector("#pingSelected").textContent'),/Core A/);
    const original=await span();
    await scroll(overview);
    const point=await pos(overview,spike(original));
    const beforeHover=calls.length;
    await mouse("mouseMoved",point);
    await wait('document.querySelector("#pingOverviewChart .ping-tooltip").textContent.includes("128.237 ms")');
    assert.equal(calls.length,beforeHover,"hover begins no requests");
    await pointerClick(point);
    await wait(pinned+'.includes("128.237 ms")');
    assert.equal(await evaluate('document.querySelector("#pingOverviewChart .ping-pin").classList.contains("ping-plot-hidden")'),false);
    assert.equal(calls.length,beforeHover,"raw pin reuses loaded overview");
    await wait('!document.querySelector("#pingDrillStatistics").hidden');
    assert.match(await evaluate('document.querySelector("#pingDrillMetrics").textContent'),/P50.*P95.*Max.*128\.2 ms.*STDDEV.*Loss/s);
    const rawStatsTime=await evaluate('document.querySelector("#pingDrillStatistics h4").textContent');
    const mainStats=await evaluate('document.querySelector("#pingMetrics").textContent');
    checks.push("real spike hover and pin, exact 128.237 ms, synchronized fixed-window stats, zero new HTTP");

    for(const [w,h] of [[1920,1080],[1366,768],[390,844]]){
      await screen(w,h);
      await evaluate('document.querySelector("#pingWorkspace").scrollTop=0');
      const fits=await evaluate('(()=>{const e=document.querySelector("#pingWorkspace");return e.scrollWidth<=e.clientWidth&&document.documentElement.scrollWidth<=innerWidth})()');
      assert.equal(fits,true,w+" history overflow");
      await shot("ux113-history-"+w+"x"+h);
    }
    await send("Emulation.setTouchEmulationEnabled",{enabled:true,maxTouchPoints:1});
    await scroll(overview);
    const oldPinned=await evaluate(pinned);
    const touch=await pos(overview,spike(original)-2);
    await send("Input.dispatchTouchEvent",{type:"touchStart",touchPoints:[touch]});
    await send("Input.dispatchTouchEvent",{type:"touchEnd",touchPoints:[]});
    await wait(pinned+'.includes("已固定")&&'+pinned+'!=='+JSON.stringify(oldPinned));
    assert.notEqual(await evaluate('document.querySelector("#pingDrillStatistics h4").textContent'),rawStatsTime);
    assert.equal(await evaluate('document.querySelector("#pingMetrics").textContent'),mainStats);
    await send("Emulation.setTouchEmulationEnabled",{enabled:false});
    await screen(1920,1080);
    await evaluate('document.querySelector("#pingOverviewChart svg").focus()');
    await plotKey("keyDown","ArrowLeft");await plotKey("keyUp","ArrowLeft");
    await plotKey("keyDown","Enter");await plotKey("keyUp","Enter");
    await wait(pinned+'.includes("已固定")');
    await plotKey("keyDown","Escape");await plotKey("keyUp","Escape");
    assert.equal(await evaluate(pinned+'.trim()'),"");
    assert.equal(await evaluate('document.querySelector("#pingDrillStatistics").hidden'),true);
    checks.push("three history sizes, mobile touch pin, keyboard navigation/Enter/Escape");

    await scroll(overview);
    const a=await pos(overview,original.start+60), b=await pos(overview,original.start+190);
    const beforeDrag=calls.length;
    await mouse("mouseMoved",a);await mouse("mousePressed",a,{button:"left",buttons:1,clickCount:1});
    await mouse("mouseMoved",b,{button:"left",buttons:1});
    await wait('!document.querySelector("#pingOverviewChart .ping-brush").classList.contains("ping-plot-hidden")');
    assert.equal(calls.length,beforeDrag);
    await mouse("mouseReleased",b,{button:"left",buttons:0,clickCount:1});
    await wait(exists(overview)+'&&Number(document.querySelector("'+overview+'").dataset.chartEnd)-Number(document.querySelector("'+overview+'").dataset.chartStart)<150');
    const zoomed=await span();
    assert.ok(Math.abs(zoomed.start-(original.start+60))<=1);
    assert.ok(Math.abs(zoomed.end-(original.start+190))<=1);
    assert.equal(calls.length,beforeDrag+1);
    await scroll("#pingPan");
    const panNow=await evaluate('(()=>{const r=document.querySelector("#pingPan").getBoundingClientRect();return {a:{x:r.left+r.width*.25,y:r.top+r.height*.5},b:{x:r.left+r.width*.12,y:r.top+r.height*.5}}})()');
    const beforePan=calls.length;
    await mouse("mousePressed",panNow.a,{button:"left",buttons:1,clickCount:1});
    await mouse("mouseMoved",panNow.b,{button:"left",buttons:1});
    await sleep(220);assert.equal(calls.length,beforePan);
    await mouse("mouseReleased",panNow.b,{button:"left",buttons:0,clickCount:1});
    await wait(exists(overview)+'&&Number(document.querySelector("'+overview+'").dataset.chartStart)!=='+zoomed.start);
    const panned=await span();
    assert.equal(panned.end-panned.start,zoomed.end-zoomed.start);
    assert.ok(panned.start<zoomed.start);
    await click("#pingGoLater");
    await wait(exists(overview)+'&&Number(document.querySelector("'+overview+'").dataset.chartStart)>'+panned.start);
    await click("#pingPrevRange");
    await wait(exists(overview)+'&&Number(document.querySelector("'+overview+'").dataset.chartStart)==='+panned.start);
    await click("#pingResetRange");
    await wait(exists(overview)+'&&Number(document.querySelector("'+overview+'").dataset.chartStart)==='+original.start);
    checks.push("drag brush and native pan only fetch on commit, later/previous/reset and identical window span");

    await evaluate("history.back(); void 0");
    await wait(historyOff+'&&document.querySelector("#pingSearch").value==="Core"');
    await wait('document.activeElement.classList.contains("ping-card-open")');
    await evaluate("history.forward(); void 0");
    await wait(historyOn+"&&"+exists(overview));
    assert.deepEqual(await span(),original);
    { const pageStart=await evaluate("performance.timeOrigin");
    await send("Page.reload",{ignoreCache:true});
    await wait("performance.timeOrigin!=="+pageStart+"&&"+historyOn+"&&"+exists(overview)); }
    assert.deepEqual(await span(),original);
    await click("#pingBackToWall");
    await wait(historyOff+'&&document.querySelector("#pingSearch").value==="Core"&&'+cardCount+'===1');
    const key=await evaluate('document.querySelector(".ping-card").dataset.key');
    await navigate("?target="+encodeURIComponent(key)+"&at="+(original.end-900));
    await wait(historyOn+"&&"+exists(overview));
    assert.equal((await span()).end,original.end-900);
    checks.push("independent URL, browser back/forward/refresh, legacy target+at, wall focus restoration");

    for(const [label,seconds] of [["15m",900],["1h",3600],["6h",21600],["24h",86400],["7d",604800]]){
      const initial=calls.length;
      await click('[data-span="'+label+'"]');
      await wait(exists(overview)+'&&Number(document.querySelector("'+overview+'").dataset.chartEnd)-Number(document.querySelector("'+overview+'").dataset.chartStart)==='+seconds);
      const requests=calls.slice(initial);
      assert.ok(requests.every(r=>!r.raw||r.span<=3600));
      if(seconds>3600){assert.ok(requests.some(r=>r.ranged));assert.ok(requests.every(r=>!r.raw));}
    }
    await scroll(overview);
    const week=await span();
    const aggregateAt=await pos(overview,week.end-(week.end-week.start)*0.04);
    await mouse("mouseMoved",aggregateAt);
    await wait('document.querySelector("#pingOverviewChart .ping-tooltip").textContent.includes("聚合桶")');
    const beforeLong=calls.length;
    await pointerClick(aggregateAt);
    await wait(exists(drill));
    const localRange=await span(drill);
    assert.ok(localRange.end-localRange.start<=3600);
    assert.equal(calls.length,beforeLong+1);
    await scroll(drill);
    const beforeRaw=calls.length;
    await pointerClick(await pos(drill,spike(localRange)));
    await wait(pinned+'.includes("128.237 ms")');
    assert.equal(calls.length,beforeRaw,"original RTT uses its already loaded samples");
    assert.equal(await evaluate('document.querySelector("#pingDistribution .ping-pin").classList.contains("ping-plot-hidden")'),false);
    await click("#pingExport");
    let csv;
    for(let i=0;i<25&&!csv;i++){csv=(await fs.readdir(output)).find(f=>/^ping.*\.csv$/.test(f));if(!csv)await sleep(80);}
    assert.ok(csv);
    assert.match(await fs.readFile(path.join(output,csv),"utf8"),/timestamp_utc,job,name,ip,target_labels,scrape_up,probe_success,rtt_ms/);
    checks.push("5m–7d, aggregated tooltip, bounded drill, original RTT click fixed and UTC CSV");

    timeoutTrend=true;
    await click('[data-span="5m"]');
    await wait('document.querySelector("#pingOverviewChart").textContent.includes("超时")');
    timeoutTrend=false;
    await click("#pingHistoryRefresh");
    await wait(exists(overview));
    extraDelay=true;
    const abortedStart=calls.length;
    await click("#pingHistoryRefresh");await sleep(150);
    await click('[data-span="15m"]');
    await sleep(250);
    assert.ok(calls.slice(abortedStart).some(r=>r.aborted));
    extraDelay=false;
    await wait(exists(overview)+'&&Number(document.querySelector("'+overview+'").dataset.chartEnd)-Number(document.querySelector("'+overview+'").dataset.chartStart)===900');
    await navigate("?comparecheck=1");
    await wait(historyOff+'&&'+cardCount+'===100');
    await evaluate('(()=>{const a=[...document.querySelectorAll(".ping-card-compare")];for(const i of [0,2,4,a.length-1])a[i].click();})()');
    assert.equal(await evaluate('document.querySelectorAll(".ping-card-compare[aria-pressed=true]").length'),4);
    assert.ok(await evaluate('document.querySelectorAll(".ping-card-compare:disabled").length>0'));
    await click("#pingComparison");
    await wait(historyOn+"&&"+exists(overview)+'&&document.querySelectorAll(".ping-history-target").length===4');
    { const pageStart=await evaluate("performance.timeOrigin");
    await send("Page.reload",{ignoreCache:true});
    await wait("performance.timeOrigin!=="+pageStart+"&&"+historyOn+"&&"+exists(overview)+'&&document.querySelectorAll(".ping-history-target").length===4'); }
    checks.push("503 recovery, stale request cancellation, four-target bounds and reload");
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
      const samples={n,round,httpP95:pct(requests.filter(r=>!r.aborted&&r.ms!=null).map(r=>r.ms)),chromeRssMiB:await chromeRss(procAfter),coldMs:Math.round(loaded-start),screenMs:Math.round(loaded-ready),coldTrend:cold.length,statusRequests:calls.filter(e=>e.kind==="summary").length,trend:requests.length,peak,aborts:requests.filter(e=>e.aborted).length,duplicates,frames:frames.length,frameP95:pct(frames),frameMax:pct(frames,1),jsMiB:Math.round(after.JSHeapUsedSize/1048576*100)/100,mainThreadPct:Math.round(10000*(after.TaskDuration-before.TaskDuration)/(after.Timestamp-before.Timestamp))/100,chromeCpuPct:Math.round(10000*procDelta/(after.Timestamp-before.Timestamp))/100};
      bench.push(samples);
      await fs.writeFile(path.join(__dirname,"browser-progress.json"),JSON.stringify({checks,bench},null,2));
    }
    assert.deepEqual(exceptions,[]);
    const report={head,version:version.product,checks,bench,exceptions,backEnd:"synthetic HTTP"};
    await fs.writeFile(path.join(output,"ux113-browser-results.json"),JSON.stringify(report,null,2));
    return report;
  } finally {try{await send("Browser.close",{},true)}catch{chrome.kill();} server.closeAllConnections();server.close();}
}
main().then(r=>console.log("UX113_BROWSER "+JSON.stringify(r))).catch(e=>{console.error(e.stack);server.closeAllConnections();server.close();process.exitCode=1;});
