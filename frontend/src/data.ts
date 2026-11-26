import raw from '../../neurodvs_hackathon_mock/data/vehicles.json'

export type Check = {session_id:string; checked_at:string; status:string; anomaly_type:string|null; anomaly_title:string|null; description:string; signal_quality:string; confidence:number|null}
export type Vehicle = {vehicle_id:string; garage_no:string; make:string; model:string|null; engine_model:string; sessions:Check[]}
export type Message = {id:string; role:'user'|'assistant'|'diagnostic'; text:string; at:string; status?:string; source?:boolean}
export type Draft = {check:string; fault:string; diagnosis:string; priority:string; works:string; parts:string; sources:string}
export type Chat = {messages:Message[]; draft:Draft; lastCheck?:string}
export type MLResult = {vehicle_id:string; session_id:string; status:'normal'|'anomaly'|'check_failed'; anomaly_type:string|null; anomaly_title:string|null; anomaly_score:number|null; confidence:number|null; signal_quality:'good'|'warning'|'bad'; description:string; model_version:string}
export type DiagnosticChat = Chat & {diagnostics?:MLResult[]}
const blank = ():Draft => ({check:'',fault:'',diagnosis:'',priority:'medium',works:'',parts:'',sources:''})
const KEY='digital-mechanic-demo-v1'
const emptyChat = ():Chat => ({messages:[],draft:blank()})
const read = ():Record<string,Chat> => { try {return JSON.parse(localStorage.getItem(KEY) || '{}')} catch {return {}} }
const write = (data:Record<string,Chat>) => localStorage.setItem(KEY,JSON.stringify(data))
export const demo = {
 vehicles: raw.vehicles as Vehicle[],
 repairs(vehicle:string) {return vehicle === raw.vehicles[0].vehicle_id ? [{date:'2026-10-01',work:'Плановое техническое обслуживание',status:'Выполнено',document:'Демонстрационная запись'}] : []},
 chat(vehicle:string):Chat {const all=read(); if (!all[vehicle]) {all[vehicle]=emptyChat(); write(all)} return all[vehicle]},
 save(vehicle:string,chat:Chat) {const all=read(); all[vehicle]=chat; write(all)},
 draft:blank,
}
