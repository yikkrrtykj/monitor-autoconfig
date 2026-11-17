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
  const weekTargets=new Set([0,4,11,12]);
  const rows=fixture.map(device=>({device,labels:spec(device.metric)}));
  const historicalRows=rows.filter(({device})=>weekTargets.has(device.i));
  const filepath=path.join(scenario,"blackbox-history.om");
  const out=createWriteStream(filepath,{encoding:"utf8"});
  let seen=new Set(), samples=0, chunk="";
  for(let t=at-604800;t<=at;t++)for(const {device,labels} of (t<at-2400?historicalRows:rows)){
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
  const imported=await command(path.join(engine,"promtool"),["tsdb","create-blocks-from","openmetrics",filepath,dir],240000);
  await fs.writeFile(path.join(scenario,"import.txt"),imported);
  return {at,dir,seedSamples:samples,seedSeries:seen.size,seconds:604800,n};
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

async function main(){
  await fs.mkdir(evidence,{recursive:true});
  await new Promise((resolve,reject)=>exporter.listen(0,"127.0.0.1",resolve).on("error",reject));
  const fixture=devices(300);
  current=new Map(fixture.map(item=>[targetKey(item),item]));
  const scenario=path.join(__dirname,"week-history");
  await progress("seed");
  const base=await seed(300,fixture,scenario);
  await progress("start",{samples:base.seedSamples});
  await config(fixture,path.join(scenario,"prometheus.yml"));
  const instance=await start(scenario,base,1);
  const targets=[0,4,11,12].map(i=>fixture[i]).map(({interval,metric})=>({...metric,interval,metric,key:identity(metric)}));
  const rows=[], requests=[];
  let span=0;
  const query=async(expr,opts,signal)=>{
    const range=opts.start!==undefined;
    const endpoint=range?"query_range":"query";
    const started=performance.now();
    const req={span,endpoint,step:opts.step||0,ok:false};
    requests.push(req);
    try{
      const params=new URLSearchParams({query:expr,...opts,timeout:"7s"});
      const response=await fetch(instance.web+"/api/v1/"+endpoint+"?"+params,{signal:signal||AbortSignal.timeout(9000)});
      const json=await response.json();
      assert.ok(response.ok&&json.status==="success",json.error||"HTTP "+response.status);
      req.values=json.data.result.reduce((total,series)=>total+(series.values?.length||1),0);
      assert.ok(req.values>0);
      if(range)assert.ok(req.step>=Math.ceil(span/240));
      req.ok=true;
      return json.data.result;
    }finally{
      req.ms=+(performance.now()-started).toFixed(3);
    }
  };
  const data=createPingData({settings:sourceSettings,query});
  try{
    const before=await stats(instance.child.pid);
    for(const duration of [300,900,3600,21600,86400,604800]){
      span=duration;
      const raw=span<=3600;
      const requestCount=requests.length;
      let maxRows=0,minNonempty=Infinity;
      const elapsed=[];
      for(let i=0;i<40;i++){
        const end=base.at-i*13;
        const start=end-span;
        const tick=performance.now();
        const result=await (raw?data.raw:data.overview)(targets,start,end);
        elapsed.push({ok:true,ms:performance.now()-tick});
        assert.equal(result.size,4);
        for(const series of result.values()){
          const points=raw?series:series.buckets;
          maxRows=Math.max(maxRows,points.length);
          const count=raw?series.filter(x=>x.rtt!==null).length:series.points.filter(x=>x.rtt!==null).length;
          minNonempty=Math.min(minNonempty,count);
          assert.ok(count>20,"week fixture produced too few points");
          if(!raw){
            assert.equal(points[0].from,start);
            assert.equal(points.at(-1).to,end);
            assert.ok(points.length<=241);
          }
        }
      }
      const q=requests.slice(requestCount);
      assert.ok(q.every(x=>x.ok&&(!raw||x.endpoint==="query")));
      assert.ok(q.length>=40&&q.length<=120);
      rows.push({duration,raw,total:brief(elapsed),queries:q.length,prometheus:brief(q),maxRows,minNonempty});
      await progress("query",{rows});
    }
    const after=await stats(instance.child.pid);
    const report={head,targets:300,intervals:targets.map(t=>t.interval),weekSeeded:4,wallHistorySeconds:2400,samples:base.seedSamples,rssMiB:+(after.rss/1048576).toFixed(2),queryCpuSeconds:+(after.cpu-before.cpu).toFixed(2),rows};
    await fs.writeFile(path.join(evidence,"ux113-prometheus-history.json"),JSON.stringify(report,null,2));
    console.log("UX113_HISTORY "+JSON.stringify(report));
  }finally{await stop(instance);}
}
main().catch(async e=>{await progress("failed",{error:e.stack});console.error(e.stack);process.exitCode=1;}).finally(()=>{exporter.closeAllConnections();exporter.close();});
