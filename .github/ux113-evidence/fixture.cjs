"use strict";
const groups = [["infra-core-ping",2,"Core A"],["infra-fw-ping",2,"FW VIP A"],["infra-fw-unit-ping",2,"FW Unit A"],["infra-fw-unit-ping",2,"FW Unit B"],["infra-isp-ping",1,"ISP A"],["infra-isp-ping",1,"ISP B"],["infra-isp-ping",1,"ISP C"]];
const mix = [["infra-dist-ping",2,"Stage"],["infra-dist-ping",2,"Dist"],["infra-dist-ping",2,"Dist"],["infra-srv-ping",2,"Server"],["infra-ap-ping",5,"AP"],["infra-ap-ping",5,"AP"],["infra-ap-ping",5,"AP"]];
const playerJob = "player-ping";
function devices(n) {
  return Array.from({length:n+1},(_,i)=>{
    const [job,interval,prefix]=i===n?[playerJob,5,"Player-Sentinel"]:groups[i]||mix[(i-groups.length)%mix.length];
    const name=i<groups.length||i===n?prefix:prefix+" "+String(i+1).padStart(3,"0");
    const ip=i<250?"192.0.2."+(i+1):"198.51.100."+(i-249);
    return {i,interval,metric:{job,instance:name,display_name:name,target_ip:ip}};
  });
}
const modes = (item,t) => item.i===2?"down":item.i===5?"scrape":item.i===6?"missing":
  t===null?"up":Math.floor(t/item.interval)%89===4?"missing":Math.floor(t/item.interval)%97===8?"scrape":Math.floor(t/item.interval)%101===5?"down":"up";
function rtt(item,t) {
  const base=item.i===4?0.075:0.002+item.i%7*0.001;
  return (base*(1.05+0.12*(1+Math.sin(t/7+item.i)))).toFixed(6);
}
module.exports={devices,modes,rtt,playerJob};
