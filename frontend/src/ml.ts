import {demo, type DiagnosticChat, type MLResult, type Vehicle} from './data'

const inFlight = new Set<string>()
const nullableString = (v:unknown) => v === null || typeof v === 'string'
const nullableScore = (v:unknown) => v === null || typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1

export function validateResult(value:unknown, vehicleId:string, sessionId:string):MLResult {
 if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Некорректный ответ ML API')
 const r=value as Record<string,unknown>
 if (r.vehicle_id!==vehicleId || r.session_id!==sessionId) throw new Error('Идентификаторы в ответе ML API не совпадают с запросом')
 if (!['normal','anomaly','check_failed'].includes(String(r.status)) || !['good','warning','bad'].includes(String(r.signal_quality)) || !nullableString(r.anomaly_type) || (typeof r.anomaly_type==='string' && !/^F(0[1-9]|1[0-4])$/.test(r.anomaly_type)) || !nullableString(r.anomaly_title) || !nullableScore(r.anomaly_score) || !nullableScore(r.confidence) || typeof r.description!=='string' || typeof r.model_version!=='string') throw new Error('Некорректный ответ ML API')
 if (r.status==='anomaly' && (!r.anomaly_type || !r.anomaly_title)) throw new Error('Некорректный ответ ML API')
 if (r.status==='normal' && (r.anomaly_type!==null || r.anomaly_title!==null)) throw new Error('Некорректный ответ ML API')
 if (r.status==='check_failed' && (r.anomaly_type!==null || r.anomaly_title!==null || r.anomaly_score!==null || r.confidence!==null || r.signal_quality!=='bad')) throw new Error('Некорректный ответ ML API')
 return r as MLResult
}
export function resultText(r:MLResult):string {
 if(r.status==='normal')return `Отклонений не обнаружено.\n${r.description}`
 if(r.status==='check_failed')return `Диагностическая проверка не удалась.\n${r.description}`
 return `${r.anomaly_title} · ${r.anomaly_type}\n${r.description}\nУверенность модели: ${r.confidence===null?'не указана':`${Math.round(r.confidence*100)}%`}\nКачество сигнала: ${{good:'хорошее',warning:'с помехами',bad:'непригодное'}[r.signal_quality]}`
}
export async function diagnoseNext(vehicle:Vehicle):Promise<DiagnosticChat|null> {
 const id=vehicle.vehicle_id
 if(inFlight.has(id)) throw new Error('Проверка уже выполняется')
 inFlight.add(id)
 try {
  const initial=demo.chat(id) as DiagnosticChat
  const done=new Set((initial.diagnostics ?? []).map(x=>x.session_id))
  const next=vehicle.sessions.find(x=>!done.has(x.session_id))
  if(!next)return null
  let response:Response
  try { response=await fetch('/ml-api/diagnose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vehicle_id:id,session_id:next.session_id})}) }
  catch {throw new Error('Не удалось подключиться к ML API. Проверьте, запущен ли сервис.')}
  if(response.status===404){
   let detail:unknown;try {detail=(await response.json()).detail}catch{/* HTTP body is not guaranteed to be JSON */}
   if(detail==='Not Found')throw new Error('ML API не найден по настроенному адресу. Проверьте порт и перезапустите фронтенд.')
   throw new Error('Проверка или автомобиль не найдены в ML API (404).')
  }
  if(response.status===422)throw new Error('ML API отклонил параметры проверки (422).')
  if(!response.ok)throw new Error(`Ошибка ML API (HTTP ${response.status}). Попробуйте снова.`)
  let body:unknown;try {body=await response.json()}catch{throw new Error('ML API вернул некорректный JSON.')}
  const result=validateResult(body,id,next.session_id)
  const latest=demo.chat(id) as DiagnosticChat
  if((latest.diagnostics??[]).some(x=>x.session_id===result.session_id)) return latest
  const chat:DiagnosticChat={...latest,lastCheck:result.session_id,diagnostics:[...(latest.diagnostics??[]),result],messages:[...latest.messages,{id:crypto.randomUUID(),role:'diagnostic',text:resultText(result),status:result.status,at:new Date().toISOString()}]}
  demo.save(id,chat);return chat
 } finally {inFlight.delete(id)}
}
