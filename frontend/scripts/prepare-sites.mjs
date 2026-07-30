import { copyFile, mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const projectDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const serverDir = resolve(projectDir, 'dist/server')

await mkdir(serverDir, { recursive: true })
await copyFile(
  resolve(projectDir, 'server/index.js'),
  resolve(serverDir, 'index.js'),
)
