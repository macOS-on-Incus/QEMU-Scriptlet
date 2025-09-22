# Some devices don’t make a lot of sense in the macOS world, at least for now
DELETED_DEVICES = {
  'chardev': ['spice-usb-chardev1', 'spice-usb-chardev2', 'spice-usb-chardev3'],
  'device': ['gpu', 'keyboard', 'pcie8', 'pcie9', 'pcie10', 'pcie11', 'pcie12', 'spice-usb1',
             'spice-usb2', 'spice-usb3', 'tablet', 'usb']
}

# On the other hand, a few devices need to be added
ADDED_DEVICES = {
  'audiodev': {
    'snd0': {'driver': 'spice'}
  },
  'device': {
    'apple_smc': {'driver': 'isa-applesmc',
                  'osk': 'ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc'},
    'qemu_audio': {'driver': 'virtio-sound-pci', 'audiodev': 'snd0'},
    'qemu_sata': {'driver': 'ich9-ahci'},
    'qemu_vga': {'driver': 'virtio-vga'},
    'qemu_usb': {'driver': 'qemu-xhci', 'p2': '8', 'p3': '8'},
    'usb_keyboard': {'driver': 'usb-kbd', 'bus': 'qemu_usb.0'},
    'usb_tablet': {'driver': 'usb-tablet', 'bus': 'qemu_usb.0'}
  }
}


def remap_storage(dev, drive):
  """
  Remap a storage device onto a SATA port
  :param dev: The dictionary representing the original device
  :param drive: The SATA drive
  """
  # Get data from the device
  qdev = dev['qdev']
  inserted = dev['inserted']
  fdset = 'fdset{}'.format(inserted['file'].split('/')[-1])

  log_info('[macOS scriptlet] Remapping disk {} to {}'.format(inserted['node-name'], drive))

  # Add a blockdev with the same FDset
  run_qmp({'execute': 'blockdev-add',
           'arguments': {'aio': 'native',
                         'cache': {'direct': True, 'no-flush': False},
                         'discard': 'unmap',
                         'driver': inserted['drv'],
                         'filename': inserted['file'],
                         'locking': 'off',
                         'node-name': fdset,
                         'read-only': inserted['ro']}})

  # Attach this blockdev to the SATA drive
  qom_set(path='/machine/peripheral/{}'.format(drive), property='drive', value=fdset)

  # Unplug the original device
  if qdev.endswith('/virtio-backend'):
    qdev = qdev[:-15]
  log_info('[macOS scriptlet] Unplugging {}'.format(qdev))
  device_del(id=qdev)


def hmp(command):
  """
  Run an HMP command
  :param command: The command to execute
  """
  return run_qmp({'execute': 'human-monitor-command',
                  'arguments': {'command-line': command}})['return'].strip().split('\r\n')


def remap_network(netdev, dev_name, net_id, fds):
  """
  Remap a network device onto a USB card
  :param netdev: The original netdev name
  :param dev_name: The original device name
  :param net_id: The USB card number
  :param fds: The TAP FDs
  """
  # Get data from the device
  mac = qom_get(path='/machine/peripheral/{}'.format(dev_name), property='mac')
  name = 'net{}'.format(net_id)

  log_warn('[macOS scriptlet] Remapping NIC {} [{}] to {}; consider setting `io.bus: usb`'
           .format(netdev, mac, name))

  # Add a netdev with the same FDs
  netdev_add(type='tap', id=name, fds=':'.join(fds))

  # Attach this netdev to a new USB card
  device_add(driver='usb-net', id=name, netdev=name, mac=mac, bus='qemu_usb.0')

  # Unplug the original device
  run_command('set_link', name=dev_name, up=False)
  log_info('[macOS scriptlet] Unplugging {}'.format(dev_name))
  device_del(id=dev_name)


def patch_config(devices):
  """
  Patch QEMU configuration
  :param devices: The expanded devices dictionary
  """
  log_info('[macOS scriptlet] Reconfiguring QEMU')

  # Initialize a dummy block device for hot-remapping purposes
  set_qemu_cmdline(get_qemu_cmdline() +
                   ['-blockdev', 'node-name=devzero,driver=raw,'+
                                 'file.driver=host_device,file.filename=/dev/zero'])

  # Get initial QEMU configuration
  initial_conf = get_qemu_conf()
  conf = []

  # Remove a few unusable devices and immediately patch 9p shares
  deleted = ['{} "qemu_{}"'.format(prefix, name)
             for (prefix, devices) in DELETED_DEVICES.items() for name in devices]
  for device in initial_conf:
    name = device['name']
    if name in deleted:
      continue
    if 'driver' in device['entries'] and device['entries']['driver'] == 'virtio-9p-pci':
      device['entries'].pop('addr')
      device['entries']['bus'] = 'pcie.0'
    conf.append(device)

  # Add necessary devices
  added = {'{} "{}"'.format(prefix, name): value for (prefix, devices) in ADDED_DEVICES.items()
                                                 for (name, value) in devices.items()}
  for (name, entries) in added.items():
    conf.append({'name': name, 'entries': entries})

  # Add placeholder SATA disks
  sata_count = 0
  for (name, device) in devices.items():
    if (device['type'] == 'disk' and not name.startswith('iso-volume')
                                 and device.get('path', '/') == '/'):
      conf.append({'name': 'device "sata{}"'.format(sata_count),
                   'comment': 'Automatically generated SATA disk',
                   'entries': {'driver': 'virtio-blk-pci', 'drive': 'devzero', 'share-rw': 'on'}})
      sata_count += 1

  # Set the new configuration
  set_qemu_conf(conf)


def remap_devices():
  """Remap QEMU devices"""
  log_info('[macOS scriptlet] Remapping devices')

  # Initialize device numbers
  sata_id = 0
  net_id = 0

  # For each block device
  for dev in run_command('query-block'):
    # If the device is a non-CD-ROM Incus disk
    if dev['inserted']['node-name'].startswith('incus_') and 'tray_open' not in dev:
      # Remap it
      remap_storage(dev, 'sata{}'.format(sata_id))
      sata_id += 1

  # Scan the network FDs
  fds = {}
  for line in hmp('info network'):
    if line.startswith(' \\ '):
      netdev = line.split(':')[0][3:]
      if netdev not in fds:
        fds[netdev] = []
      if 'fd=' in line:
        fds[netdev].append(line.split('fd=')[1])

  # For each device
  for dev in qom_list(path='/machine/peripheral'):
    # If the device is a VirtIO PCI network device
    if dev['type'] == 'child<virtio-net-pci>':
      dev_name = dev['name']
      # Get its backend netdev
      netdev = qom_get(path='/machine/peripheral/{}'.format(dev_name), property='netdev')
      # And remap it
      remap_network(netdev, dev_name, net_id, fds[netdev])
      net_id += 1


def qemu_hook(instance, stage):
  """Scriptlet entry point"""
  if stage == 'config':
    patch_config(instance.expanded_devices)
  elif stage == 'pre-start':
    remap_devices()
