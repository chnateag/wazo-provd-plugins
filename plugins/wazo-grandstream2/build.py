# -*- coding: utf-8 -*-

# Copyright 2013-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

# Depends on the following external programs:
#  -rsync

from subprocess import check_call


@target('1.0.11.48', 'wazo-grandstream2-1.0.11.48')
def build_1_0_11_48(path):
    check_call(
        [
            'rsync',
            '-rlp',
            '--exclude',
            '.*',
            '--include',
            '/templates/*',
            'common/',
            path,
        ]
    )

    check_call(['rsync', '-rlp', '--exclude', '.*', '1.0.11.48/', path])

