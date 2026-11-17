"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs/promises");
const {createWriteStream}=require("node:fs");
const net=require("node:net");
const http=require("node:http");
const os=require("node:os");
const path=require("node:path");
const cp=require("node:child_process");
const {finished}=require("node:stream/promises");
const {once}=require("node:events");
const {devices,modes,rtt,playerJob}=require("./fixture.cjs");
const {createPingData}=require(path.join(process.env.PR112_BIGSCREEN,"ping/data.js"));
const {policy,identity}=require(path.join(process.env.PR112_BIGSCREEN,"ping/model.js"));
const engine=process.env.PR112_PROM;
const evidence=path.join(__dirname,"evidence");
const sleep=ms=>new Promise(r=>setTimeout(r,Math.max(0,ms)));
const head="73b75a296c2cade44a3374c372c642ac69c39dc7";
const sourceSettings=policy({});
let current=new Map();
let exporterRequests={};
const exporter=http.createServer((req,res)=>{
  const url=new URL(req.url,"http://127.0.0.1");
  const device=current.get(url.searchParams.get("target"));
  if(url.pathname!=="/probe"||!device){res.writeHead(404).end();return;}
  const outcome=modes(device,Math.floor(Date.now()/1000));
  exporterRequests[outcome]=(exporterRequests[outcome]||0)+1;
  if(outcome==="scrape"||outcome==="missing"){res.writeHead(503).end();return;}
  let payload="probe_success "+(outcome==="up"?1:0)+"\n";
  if(outcome==="up")payload+='probe_icmp_duration_seconds{phase="rtt"} '+rtt(device,Date.now()/1000)+"\n";
  res.writeHead(200,{"content-type":"text/plain; version=0.0.4"}).end(payload);
});
function command(exe,args,timeout=60000) {
  return new Promise((resolve,reject)=>cp.execFile(exe,args,{cwd:__dirname,windowsHide:true,timeout,maxBuffer:1024*1024},(error,stdout,stderr)=>
    error?reject(new Error(exe+" "+error.message+" "+stderr.slice(-1000))):resolve(stdout.trim())));
}
const progress=(stage,detail={})=>fs.writeFile(path.join(__dirname,"prom-progress.json"),JSON.stringify({stage,...detail}));
async function port() {
  const socket=net.createServer();
  await new Promise((r,j)=>socket.listen(0,"127.0.0.1",r).on("error",j));const p=socket.address().port;
  await new Promise(r=>socket.close(r));return p;
}
function spec(metric) {return Object.entries(metric).map(([name,value])=>name+"="+JSON.stringify(value)).join(",");}
async function seed(n,fixture,scenario) {
  await fs.mkdir(scenario,{recursive:true});
  const at=Math.floor(Date.now()/1000)-35;
  const rows=fixture.map(device=>({device,labels:spec(device.metric)}));
  const filepath=path.join(scenario,"blackbox-history.om");
  const out=createWriteStream(filepath,{encoding:"utf8"});
  let seen=new Set(), samples=0, chunk="";
  for(let t=at-2400;t<=at;t++)for(const {device,labels} of rows){
    if(t%device.interval)continue;
    const mode=modes(device,t);
    // Retain old inventory evidence for a disappeared target.
    if(mode==="missing" && !(device.i===6 && t<at-600))continue;
    chunk+="up{"+labels+"} "+(mode==="scrape"?0:1)+" "+t+"\n";
    samples++;seen.add("up"+device.i);
    if(mode!=="scrape"){
      chunk+="probe_success{"+labels+"} "+(mode==="down"?0:1)+" "+t+"\n";
      samples++;seen.add("probe"+device.i);
    }
    if(mode==="up"){
      chunk+='probe_icmp_duration_seconds{phase="rtt",'+labels+"} "+rtt(device,t)+" "+t+"\n";
      samples++;seen.add("rtt"+device.i);
    }
    if(chunk.length>=800000){if(!out.write(chunk))await once(out,"drain");chunk="";}
  }
  out.end(chunk+"# EOF\n");await finished(out);
  const dir=path.join(scenario,"seed");
  await fs.mkdir(dir,{recursive:true});
  const imported=await command(path.join(engine,"promtool"),["tsdb","create-blocks-from","openmetrics",filepath,dir],125000);
  await fs.writeFile(path.join(scenario,"import.txt"),imported);
  return {at,dir,seedSamples:samples,seedSeries:seen.size,seconds:2400,n};
}
async function config(fixture,filepath) {
  const groups=new Map(), url="127.0.0.1:"+exporter.address().port;
  for(const item of fixture){const group=groups.get(item.metric.job)||[];group.push(item);groups.set(item.metric.job,group);}
  const lines=["global:","  scrape_interval: 5s","scrape_configs:"];
  for(const [job,targets] of groups){
    lines.push("  - job_name: "+JSON.stringify(job),"    scrape_interval: "+targets[0].interval+"s","    scrape_timeout: 950ms",
      "    metrics_path: /probe","    static_configs:");
    for(const {metric:{job, ...labels}} of targets){
      lines.push("      - targets: ["+JSON.stringify(url)+"]","        labels:");
      for(const [name,value] of Object.entries(labels))lines.push("          "+name+": "+JSON.stringify(value));
      // Parameter via relabel instead of embedding synthetic addresses in the socket.
    }
    lines.push("    relabel_configs:","      - source_labels: [instance]","        target_label: __param_target");
  }
  await fs.writeFile(filepath,lines.join("\n")+"\n");
}
async function start(scenario,seed,round) {
  const web="http://127.0.0.1:"+await port();
  const data=path.join(scenario,"tsdb-"+round);
  await fs.cp(seed.dir,data,{recursive:true});
  const log=await fs.open(path.join(scenario,"prom-"+round+".log"),"w");
  const child=cp.spawn(path.join(engine,"prometheus"),["--config.file="+path.join(scenario,"prometheus.yml"),"--storage.tsdb.path="+data,
    "--web.listen-address="+web.slice(7),"--web.enable-lifecycle","--query.timeout=7s","--log.level=warn"],
    {cwd:scenario,windowsHide:true,stdio:["ignore",log.fd,log.fd]});
  await log.close();
  for(let i=0;i<120;i++){
    if(child.exitCode!==null)throw Error("Prometheus stopped during startup: "+child.exitCode);
    if(await fetch(web+"/-/ready",{signal:AbortSignal.timeout(800)}).then(r=>r.ok,()=>false))return {web,child};
    await sleep(100);
  }
  child.kill();throw Error("Prometheus readiness timeout");
}
async function stop(instance){
  const exited=once(instance.child,"exit");
  if(instance.child.exitCode!==null)return;
  await fetch(instance.web+"/-/quit",{method:"POST",signal:AbortSignal.timeout(1500)}).catch(()=>null);
  await Promise.race([exited,sleep(2000)]);
  if(instance.child.exitCode===null){instance.child.kill();await exited;}
}
let hz;
async function stats(pid) {
  hz ??= Number(await command("getconf",["CLK_TCK"],5000));
  const [stat,status]=await Promise.all([fs.readFile("/proc/"+pid+"/stat","utf8"),fs.readFile("/proc/"+pid+"/status","utf8")]);
  const fields=stat.slice(stat.lastIndexOf(")")+2).trim().split(/\s+/);
  return {cpu:(Number(fields[11])+Number(fields[12]))/hz,rss:Number(status.match(/^VmRSS:\s+(\d+)/m)?.[1])*1024};
}
async function api(web,expr,params={}) {
  const url=web+"/api/v1/query?"+new URLSearchParams({query:expr,timeout:"7s",...params});
  const response=await fetch(url,{signal:AbortSignal.timeout(9000)});
  return {status:response.status,payload:await response.json()};
}
const classify=expr=>expr.includes('"target"')?(expr.includes("offset 1800s")&&expr.includes("[1800s]")?"summary":"invalid"):"trend";
const targetKey=device=>device.metric.instance;
function batch(fixture,seed,number){
  const available=fixture.filter(x=>x.metric.job!==playerJob);
  const width=Math.min(12,available.length);
  const rotation=(number*11)%(available.length-width*2+1);
  const target=d=>({...d.metric,metric:d.metric,key:identity(d.metric),interval:d.interval});
  return [available.slice(rotation,rotation+width).map(target),available.slice(rotation+width,rotation+width*2).map(target)];
}
const quantile=(a,p)=>a.length?+(a[Math.ceil(a.length*p)-1]).toFixed(3):null;
function brief(rows){
 const values=rows.filter(r=>r.ok).map(r=>r.ms).sort((a,b)=>a-b);
 return {attempts:rows.length,errors:rows.length-values.length,p50:quantile(values,.5),p95:quantile(values,.95),max:quantile(values,1)};
}
const allReports=[];
const timeoutChecks=[];
const cancelChecks=[];
const allSamples=[];
async function runRound(n,fixture,base,round,scenario){
  const instance=await start(scenario,base,round);
  const {web,child}=instance;
  const own=[], pools=new Map(), examples={};
  let phase="cold", concurrent=0, trendPeak=0;
  let clock=base.at;
  const query=async(expr,opts,signal)=>{
    const kind=classify(expr);
    assert.ok(kind!=="invalid","uses complete 30m inventory and 5m offset");
    const record={n,round,phase,kind,at:opts.time,ok:false};
    own.push(record);const begin=performance.now();
    if(kind==="trend"){concurrent++;trendPeak=Math.max(trendPeak,concurrent);}
    try {
      const url=web+"/api/v1/query?"+new URLSearchParams({query:expr,...opts,timeout:"7s"});
      const response=await fetch(url,{signal:AbortSignal.any([signal||new AbortController().signal,AbortSignal.timeout(9000)])});
      const payload=await response.json();
      if(!response.ok||payload.status!=="success"||!Array.isArray(payload.data?.result))throw Error("HTTP "+response.status+" "+(payload.errorType||"invalid query result"));
      record.ok=true;
      record.series=payload.data.result.length;
      if(!examples[kind])examples[kind]=expr;
      return payload.data.result;
    }catch(e){record.error=e.name+": "+e.message;throw e;}
    finally{record.ms=Math.round((performance.now()-begin)*1000)/1000;if(kind==="trend")concurrent--;}
  };
  const data=createPingData({settings:sourceSettings, query});
  async function summary(at){
    try {
      const result=await data.summary(at);
      assert.equal(result.targets.length,n);
      assert.ok(result.targets.every(x=>x.job!==playerJob));
    }catch(e){if(e.name==="AssertionError" || own.findLast(e=>e.kind==="summary")?.ok)throw e;}
  }
  async function trends(at,k){
    const groups=batch(fixture,base,k);
    const results=await Promise.allSettled(groups.map(g=>data.raw(g,at-300,at)));
    for(const [i,result] of results.entries()){
      if(result.status==="fulfilled"){assert.equal(result.value.size,groups[i].length);assert.ok([...result.value.values()].some(rows=>rows.length>30),"real matrix missing");}
    }
  }
  try{
    // First query immediately after ready. The copied blocks may remain in the OS page cache.
    await summary(clock);
    await trends(clock,0);
    phase="warm";
    for(let i=0;i<40;i++){
      clock=base.at-240+i*5;
      await summary(clock);
      if(i%2===0)await trends(clock,i+round);
    }
    phase="sustained";
    await progress("sustained",{n,round,complete:allReports.length});
    exporterRequests={};
    const procStart=await stats(child.pid), sampled=[procStart.rss];
    const start=performance.now();
    for(let sec=0;sec<45;sec+=5){
      await sleep(start+sec*1000-performance.now());
      const at=Math.floor(Date.now()/1000);
      const items=[summary(at)];
      if(sec%30===0)items.push(trends(Math.floor(at/30)*30,sec+round));
      await Promise.all(items);
      if(sec===15||sec===30)sampled.push((await stats(child.pid)).rss);
    }
    await sleep(start+45000-performance.now());
    const scrapeCounts={...exporterRequests};
    const end=await stats(child.pid), wall=(performance.now()-start)/1000;
    sampled.push(end.rss);
    const tsdb=await fetch(web+"/api/v1/status/tsdb",{signal:AbortSignal.timeout(5000)}).then(r=>r.json());
    const source='job=~"infra-.*-ping|player-ping"';
    const metrics={
      sampleSeries:'count({__name__=~"up|probe_success|probe_icmp_duration_seconds",'+source+'})',
      connections:'count(up{'+source+'})',
      scrapeP95:'quantile(0.95,scrape_duration_seconds{'+source+'})',
      scrapeMax:'max(scrape_duration_seconds{'+source+'})',
      failedScrapes:'sum(count_over_time(up{'+source+'}[45s])-sum_over_time(up{'+source+'}[45s]))',
      allScrapes:'sum(count_over_time(up{'+source+'}[45s]))'
    };
    const measured=Object.fromEntries(await Promise.all(Object.entries(metrics).map(async([k,q])=>{
      const x=await api(web,q);return [k,Number(x.payload.data?.result?.[0]?.value?.[1])];
    })));
    assert.equal(measured.connections,n+1);
    const resources={n,round,promCpuPctOneCore:+((end.cpu-procStart.cpu)*100/wall).toFixed(2),
      rssPeakMiB:+(Math.max(...sampled)/1048576).toFixed(2),headSeries:tsdb.data?.headStats?.numSeries,
      liveProbeSeries:measured.sampleSeries,scrapeP95Ms:+(measured.scrapeP95*1000).toFixed(3),scrapeMaxMs:+(measured.scrapeMax*1000).toFixed(3),
      scrapeErrors:measured.failedScrapes,scrapes:measured.allScrapes,exporter: scrapeCounts,trendPeak};
    allReports.push(resources);
    console.log("AUDIT_ROUND "+JSON.stringify({resources,summary:brief(own.filter(r=>r.kind==="summary")),trend:brief(own.filter(r=>r.kind==="trend"))}));
    allSamples.push(...own);
    if(round===1){
      for(const kind of ["summary","trend"]){
        const attempts=[];
        for(const span of [30,60,120,180]){
          const stress='sum_over_time((last_over_time(up{job=~"infra-.*-ping"}[1800s]))['+span+'s:1s])';
          const bad=await api(web,stress,{time:base.at,timeout:"1ms"});
          attempts.push({span,http:bad.status,type:bad.payload.errorType||"success",error:bad.payload.error});
          if(bad.payload.errorType==="timeout")break;
        }
        const good=await api(web,examples[kind],{time:base.at});
        assert.equal(good.payload.status,"success");
        timeoutChecks.push({n,kind,attempts,recoveryHttp:good.status,timeoutObserved:attempts.some(a=>a.type==="timeout")});
      }
      const signal=new AbortController();signal.abort();
      let aborted=false;
      try{ await data.summary(base.at,signal.signal); }catch(e){aborted=e.name==="AbortError";}
      cancelChecks.push({n,aborted,mode:"pre-aborted HTTP client; in-flight filtering/navigation is measured separately in Chromium"});
    }
    await fs.writeFile(path.join(__dirname,"prom-partial.json"),JSON.stringify({allReports,allSamples,timeoutChecks,cancelChecks}));
  }finally{await stop(instance);}
}
async function main(){
  await fs.mkdir(evidence,{recursive:true});
  const version=await command(path.join(engine,"prometheus"),["--version"],10000);
  assert.match(version,/version 2\.53\.1/);
  await new Promise((r,j)=>exporter.listen(0,"127.0.0.1",r).on("error",j));
  const datasets=[];
  const selected = Number(process.env.PR112_N);
  assert.ok([50,100,300].includes(selected));
  for(const n of [selected]){
    await progress("seed",{n});
    const fixture=devices(n);
    current=new Map(fixture.map(d=>[targetKey(d),d]));
    const scenario=path.join(__dirname,"prom-"+n);
    const base=await seed(n,fixture,scenario);
    datasets.push({n,seconds:base.seconds,seedSamples:base.seedSamples,seedSeries:base.seedSeries,
      intervals:Object.fromEntries([1,2,5].map(i=>[i,fixture.filter(d=>d.metric.job!==playerJob&&d.interval===i).length]))});
    await config(fixture,path.join(scenario,"prometheus.yml"));
    for(let round=1;round<=3;round++)await runRound(n,fixture,base,round,scenario);
  }
  const byN=datasets.map(dataset=>({dataset,phases:Object.fromEntries(["cold","warm","sustained"].map(phase=>[
    phase,Object.fromEntries(["summary","trend"].map(kind=>[kind,brief(allSamples.filter(r=>r.n===dataset.n&&r.phase===phase&&r.kind===kind))]))
  ]))}));
  const report={head,version:version.split("\n")[0],cpuModel:os.cpus()[0]?.model,logicalCpus:os.availableParallelism(),host:"GitHub-hosted Ubuntu 24.04 x64",backend:"Prometheus native engine with imported Blackbox-shaped TSDB and loopback simulated exporter",datasets:byN,resources:allReports,timeoutChecks,cancelChecks,
    notes:["Three rounds per scale. Cold process start after a seed TSDB copy; OS cache was not flushed.", "Warm: 120 requests per kind across three rounds. Sustained: three 45-second runs, 5s summary and 30s two-batch trends.", "Query HTTP timing includes reading JSON, excludes client model transforms and telemetry scrape. CPU percent is relative to one logical core; RSS is maximum sampled working set.", "Synthetic exporter intentionally emits failed scrapes; diagnostic timeout injections are excluded from the error rate."]};
  await fs.writeFile(path.join(evidence,"pr112-prometheus-results.json"),JSON.stringify(report,null,2));
  const header="n,round,phase,kind,epoch,elapsed_ms,success,series,error\n";
  await fs.writeFile(path.join(evidence,"pr112-prometheus-samples.csv"),header+allSamples.map(x=>[x.n,x.round,x.phase,x.kind,x.at,x.ms,x.ok,x.series??"",JSON.stringify(x.error??"")].join(",")).join("\n")+"\n");
  await progress("complete",{rounds:allReports.length,samples:allSamples.length});
  return report;
}
main().then(r=>console.log(JSON.stringify({status:"PASS",counts:r.datasets.map(x=>x.dataset.n)}))).catch(async e=>{await progress("failed",{error:e.stack});console.error(e.stack);process.exitCode=1;}).finally(()=>{exporter.closeAllConnections();exporter.close();});
