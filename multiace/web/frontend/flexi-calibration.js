// @MULTIACE_FLEXI_HOOK Flexi-owned module. Implements or supports the
// optional fourth operating mode; original MultiACE modes must not depend on it.
/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Pure wizard view helpers. Importing this module never sends requests. */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.FlexiCalibration=api;})(typeof globalThis!=='undefined'?globalThis:this,function(){
 'use strict';
 function sourceKey(ace,slot){if(!Number.isInteger(ace)||ace<0||ace>3||!Number.isInteger(slot)||slot<0||slot>3)throw Error('Choose a valid ACE and zero-based slot');return `${ace}:${slot}`;}
 function destination(routes,ace,slot){const h=routes[sourceKey(ace,slot)];if(!Number.isInteger(h)||h<0||h>3)throw Error('This source has no configured route');return h;}
 function resolved(profiles,key,head){const heads=profiles?.heads||{},routes=profiles?.routes||{};return {...(heads[String(head)]||{}),...(routes[key]||{})};}
 function fieldState(profiles,key,head,field){const route=profiles?.routes?.[key]||{},defaults=profiles?.heads?.[String(head)]||{};if(Object.prototype.hasOwnProperty.call(route,field))return {value:route[field],origin:route[field]===null?'explicitly unset':'route override'};return {value:defaults[field]??null,origin:Object.prototype.hasOwnProperty.call(defaults,field)?'toolhead default':'unknown'};}
 function layout(routes,selected,unitSlots=[]){
   const slots=new Map();
   for(const entry of unitSlots||[]){const ace=Number(entry.ace ?? entry.idx),count=Number(entry.slotCount ?? entry.slot_count ?? entry.slots?.length);if(Number.isInteger(ace)&&ace>=0&&ace<4)slots.set(ace,Math.max(1,Math.min(4,Number.isInteger(count)&&count>0?count:4)));}
   const lines=[];
   for(const [key,h] of Object.entries(routes||{})){
     const match=/^([0-3]):([0-3])$/.exec(key);
     if(!match||!Number.isInteger(h)||h<0||h>3)throw Error('Invalid ACE routing diagram');
     const ace=Number(match[1]),slot=Number(match[2]);
     if(!slots.has(ace))slots.set(ace,4);
     const unitTop=25+ace*180, y=unitTop+52+slot*35, hy=65+h*100;
     lines.push({key,ace,slot,head:h,y,hy,selected:key===selected,path:`M 240 ${y} C 420 ${y}, 480 ${hy}, 650 ${hy}`});
   }
   const units=[...slots.entries()].sort((a,b)=>a[0]-b[0]).map(([ace,slotCount])=>({ace,slotCount,y:25+ace*180,height:42+slotCount*35,slots:Array.from({length:slotCount},(_,slot)=>({slot,y:25+ace*180+52+slot*35,label:slot}))}));
   const heads=[0,1,2,3].map(head=>({head,y:40+head*100}));
   return {lines,units,heads,height:Math.max(420,...units.map(unit=>unit.y+unit.height+25),385)};
 }
 function diagram(routes,selected,unitSlots=[]){return layout(routes,selected,unitSlots).lines;}
  function selection(draft,routes){const ace=Number(draft.ace),slot=Number(draft.slot),head=Number(draft.head);if(draft.ace===''||draft.slot===''||draft.head==='')throw Error('Select ACE, slot and toolhead');if(!Number.isInteger(ace)||ace<0||ace>3||!Number.isInteger(slot)||slot<0||slot>3)throw Error('ACE and slot must be within the four-unit, four-slot routing range');if(destination(routes,ace,slot)!==head)throw Error('Toolhead conflicts with configured physical routing');if(!draft.material?.trim())throw Error('Confirm the actual filament material');const transportOnly=draft.transport_only===true;if(transportOnly&&draft.operation!=='load')throw Error('Transport-only qualification requires Load');return {action:'select',source:[ace,slot],head,material:draft.material.trim(),operation:draft.operation,...(transportOnly?{transport_only:true}:{})};}
 function increment(mm){if(!Number.isFinite(mm)||mm<=0||mm>5)throw Error('A single step must be greater than zero and at most 5 mm');return {action:'increment',max_mm:mm,confirmation:'APPROVED_SINGLE_CALIBRATION_STEP'};}
  function segment(mm){if(!Number.isFinite(mm)||mm<=0||mm>200)throw Error('A progressive segment must be greater than zero and at most 200 mm');return {action:'segment',max_mm:mm,confirmation:'APPROVED_CALIBRATION_SEGMENT'};}
 function canIncrement(wizard,workflow,authorized){const session=wizard?.session,job=workflow?.job;return !!(authorized&&wizard.step_ready!==false&&session&&!session.stale&&session.step===4&&job?.calibration_test&&!job.failed&&!wizard.pending_step&&['feed_path','seat','prime','purge','tipform','retract_path'].includes(job.phase));}
  function canSegment(wizard,workflow,authorized){const session=wizard?.session,job=workflow?.job;return !!(authorized&&wizard.step_ready!==false&&session&&!session.stale&&session.step===4&&job?.calibration_test&&!job.failed&&!wizard.pending_step&&job.phase==='feed_path');}
  return {sourceKey,destination,resolved,fieldState,layout,diagram,selection,increment,segment,canIncrement,canSegment};
});
