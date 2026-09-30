# -*- coding: utf-8 -*-
#
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2024 DOTSPRIME SYSTEM LLP
#    Email : sales@dotsprime.com / dotsprime@gmail.com
########################################################

class ZKError(Exception):
    pass


class ZKErrorResponse(ZKError):
    pass


class ZKNetworkError(ZKError):
    pass
