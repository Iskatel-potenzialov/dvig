import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig(({mode}) => {
 const env=loadEnv(mode, process.cwd(), 'ML_API_')
 return { plugins: [react()], server: { fs: { allow: ['..'] }, proxy: {'/ml-api': {target:env.ML_API_TARGET || 'http://127.0.0.1:18371',changeOrigin:true,rewrite:path=>path.replace(/^\/ml-api/,'')}} } }
})
