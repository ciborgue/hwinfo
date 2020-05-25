#!/usr/bin/env python3
import asyncio
from datetime import datetime, timezone
import os
import re
import json
import time
from functools import reduce
from argparse import ArgumentParser


class LogSensors:
    def __init__(self, args):
        self.args = args

        with open(args.config) as config:
            self.config = json.load(config)

        self.data = list()

    async def select_readings(self, sensors_data, sensor_device):
        if len(sensor_device) == 1:
            return [
                    sensors_data[key]
                    for key in sensors_data.keys()
                    if re.match(sensor_device[0], key)
                    ]
        return reduce(lambda a, b: a + b, [
            await self.select_readings(sensors_data[key], sensor_device[1:])
            for key in sensors_data.keys()
            if re.match(sensor_device[0], key)
            ])

    async def format_message(self, message):
        if 'Facility' in message:
            value = message['Facility'] << 3
        else:
            value = 3 << 3  # DAEMON

        if 'Priority' in message:
            value |= message['Priority']
        else:
            value |= 6  # INFO

        timereported = datetime.now(timezone.utc)

        value = [
                timereported.strftime(f'<{value}>%b %d %X'),
                os.uname().nodename,  # hostname
                f'{os.path.basename(__file__)}[{os.getpid()}]:',
                ]
        value.append('@cee:')

        message.update({
            'hostname': value[1],
            'timereported': timereported.astimezone().isoformat(),
            message['name']:
            reduce(lambda a, b: a + b,
                   message['readings']) / len(message['readings']),
            })

        del message['readings']

        value.append(json.dumps(message))

        return ' '.join(value)

    async def load_disk(self, disk_name):
        if disk_name in self.config['disk_devices']:
            device = self.config['disk_devices'][disk_name]
        else:
            device = self.config['disk_devices']['__default__']

        if 'disk_kinds' in self.config \
                and disk_name in self.config['disk_kinds']:
            kind = f'-d {self.config["disk_kinds"][disk_name]}'
        else:
            kind = ''

        sensors = await asyncio.create_subprocess_shell(
                f'sudo smartctl -Ai {kind} {device}',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
                )
        stdout, stderr = await sensors.communicate()

        message = {
                'name': disk_name,
                }

        for line in stdout.splitlines():
            line = line.decode().split()
            if line and line[0] in ['190', '194']:
                message['readings'] = [int(line[9])]
            if line and line[0] == 'Device' and line[1] == 'Model:':
                message['model'] = ' '.join(line[2:])
            if line and line[0] == 'Serial' and line[1] == 'Number:':
                message['serial'] = ' '.join(line[2:])

        if 'readings' in message:
            self.data.append(await self.format_message(message))

    async def load_sensors(self):
        sensors = await asyncio.create_subprocess_shell(
                "sensors -j",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
                )
        stdout, stderr = await sensors.communicate()

        sensor_data = json.loads(stdout.decode())

        for sensor_name in self.config["sensor_names"]:
            message = {
                    'name': sensor_name,
                    'readings': await self.select_readings(
                        sensor_data,
                        self.config['sensor_devices'][sensor_name]
                        )
                    }

            self.data.append(await self.format_message(message))

    async def send_data(self):
        if not self.data:
            return

        if self.args.dry_run:
            print(json.dumps(self.data, indent=2))
            self.data.clear()
            return

        reader, writer = await asyncio.open_connection(
                self.config['logger_addr'], self.config['logger_port'])

        while self.data:
            writer.write(f'{self.data.pop(0)}\n'.encode())
            await writer.drain()

        writer.close()
        await writer.wait_closed()

    async def main(self):
        while True:
            await asyncio.gather(
                    *(
                        [
                            self.load_disk(name)
                            for name
                            in self.config["disk_names"]
                            ] +
                        [
                            self.load_sensors(),
                            self.send_data(),
                            ]

                        )
                    )
            await asyncio.sleep(self.config['INTERVAL'] -
                                time.time() % self.config['INTERVAL'])


parser = ArgumentParser()

parser.set_defaults(dry_run=False)
parser.add_argument('-c', '--config', type=str)
parser.add_argument('-d', '--dry-run', dest='dry_run', action='store_true')

asyncio.run(LogSensors(parser.parse_args()).main())
