import { describe, expect, it } from 'vitest'
import { collectVideoHardwareInventory, normalizeWindowsVideoInventory } from './hardware-inventory'

describe('Windows video hardware inventory', () => {
  it('hashes the instance id and exposes only bounded hardware evidence', () => {
    const rawInstanceId = 'USB\\VID_2B7E&PID_B557\\PRIVATE-SERIAL-1234'
    const items = normalizeWindowsVideoInventory([{
      name: 'USB Camera', manufacturer: 'Example Vendor', instanceId: rawInstanceId,
      driverVersion: '10.0.1.2', driverProvider: 'Example Driver', driverDate: '2026-08-20T00:00:00Z',
    }])

    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ usb_vid: '2B7E', usb_pid: 'B557', transport_hint: 'usb' })
    expect(items[0].instance_id_hash).toMatch(/^[0-9a-f]{64}$/)
    expect(JSON.stringify(items)).not.toContain('PRIVATE-SERIAL-1234')
    expect(JSON.stringify(items)).not.toContain(rawInstanceId)
  })

  it('drops malformed rows and reports unsupported platforms without invoking Windows tools', async () => {
    expect(normalizeWindowsVideoInventory([{ name: 'No identity' }, null, 'bad'])).toEqual([])
    await expect(collectVideoHardwareInventory('darwin')).resolves.toEqual({
      supported: false, source: 'unsupported_platform', items: [],
    })
  })

  it.runIf(process.platform === 'win32')('executes the fixed read-only inventory on the Windows runner', async () => {
    const inventory = await collectVideoHardwareInventory()
    expect(inventory).toMatchObject({ supported: true, source: 'windows_pnp_cim_v1' })
    expect(inventory.error_code).toBeUndefined()
    for (const item of inventory.items) {
      expect(item.instance_id_hash).toMatch(/^[0-9a-f]{64}$/)
      expect(item.name.length).toBeGreaterThan(0)
      expect(JSON.stringify(item)).not.toMatch(/PRIVATE-SERIAL|\\\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}\\\\/i)
    }
  })
})
