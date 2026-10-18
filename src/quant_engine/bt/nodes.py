"""行為樹基礎節點：Selector（OR）/ Sequence（AND）。

每個 tick() 只依賴 Blackboard，無外部副作用、可完全脫線測試。
"""
from enum import Enum
from typing import List

from ..blackboard import Blackboard


class NodeStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class BTNode:
    def tick(self, bb: Blackboard) -> NodeStatus:
        raise NotImplementedError


class Selector(BTNode):
    """OR 節點：任一子節點成功即返回"""

    def __init__(self, children: List[BTNode]):
        self.children = children

    def tick(self, bb: Blackboard) -> NodeStatus:
        for child in self.children:
            if child.tick(bb) == NodeStatus.SUCCESS:
                return NodeStatus.SUCCESS
        return NodeStatus.FAILURE


class Sequence(BTNode):
    """AND 節點：所有子節點成功才返回"""

    def __init__(self, children: List[BTNode]):
        self.children = children

    def tick(self, bb: Blackboard) -> NodeStatus:
        for child in self.children:
            if child.tick(bb) == NodeStatus.FAILURE:
                return NodeStatus.FAILURE
        return NodeStatus.SUCCESS
