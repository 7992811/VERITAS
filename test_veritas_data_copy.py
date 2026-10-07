"""Differential copy tests: no market assumptions or performance thresholds."""
import copy
from datetime import datetime, timezone
from decimal import Decimal
import random
import unittest
from veritas_data_copy import deepcopy


class Custom:
    def __init__(self, value): self.value=value
    def __deepcopy__(self,memo):
        result=type(self).__new__(type(self));memo[id(self)]=result
        result.value=copy.deepcopy(self.value,memo)
        return result


class DictSubclass(dict): pass
class ListSubclass(list): pass
class StrSubclass(str): pass


class DataCopyTests(unittest.TestCase):
    def test_native_values_and_aliases_match_standard(self):
        shared={'price':12.727,'bars':[1.,2.,3.]}
        root={'first':shared,'second':shared,'source':{'name':'TBANK'},'empty':[]}
        for fn in (copy.deepcopy,deepcopy):
            result=fn(root)
            self.assertEqual(result,root)
            self.assertIs(result['first'],result['second'])
            self.assertIsNot(result['first'],root['first'])
            result['first']['bars'].append(4.)
            self.assertEqual(root['first']['bars'],[1.,2.,3.])

    def test_cycles_and_tuple_bridge_match_standard(self):
        child=[];bridge=(child,);child.append(bridge)
        root={'child':child,'bridge':bridge};root['self']=root
        for fn in(copy.deepcopy,deepcopy):
            result=fn(root)
            self.assertIs(result['self'],result)
            self.assertIs(result['bridge'],result['child'][0])
            self.assertIs(result['bridge'][0],result['child'])
            self.assertIsNot(result['child'],child)

    def test_atomic_identity_signed_zero_nan_and_large_integers(self):
        values=[None,True,False,0,10**200,12.693,-0.,float('nan'),float('inf'),3+4j,'native',b'quote']
        result=deepcopy({'values':values})
        for old,new in zip(values,result['values']):self.assertIs(old,new)
        self.assertIsNot(values,result['values'])

    def test_subclasses_datetime_decimal_and_bytearray_keep_types(self):
        root={'dict':DictSubclass(a=[1]),'list':ListSubclass([1,2]),
              'str':StrSubclass('source'),'date':datetime(2026,10,7,tzinfo=timezone.utc),
              'decimal':Decimal('12.693'),'buffer':bytearray(b'quote'),'set':{1,2},
              'frozen':frozenset((3,4)),'tuple':('one',2)}
        expected=copy.deepcopy(root);actual=deepcopy(root)
        self.assertEqual(actual,expected)
        for key in root:self.assertIs(type(actual[key]),type(expected[key]))
        actual['buffer'][0]=65
        self.assertEqual(root['buffer'],bytearray(b'quote'))

    def test_custom_copy_protocol_shares_the_same_memo(self):
        shared={'bars':[1.,2.]};value=Custom(shared)
        root={'plain':shared,'custom':value,'alias':value}
        for fn in(copy.deepcopy,deepcopy):
            result=fn(root)
            self.assertIs(result['custom'],result['alias'])
            self.assertIs(result['plain'],result['custom'].value)
            self.assertIsNot(result['custom'],value)
            self.assertIsNot(result['plain'],shared)

    def test_explicit_memo_and_preseeded_container_replacement(self):
        original={'price':12.7};replacement={'test':'replacement'}
        memo={id(original):replacement}
        self.assertIs(deepcopy(original,memo),replacement)
        shared=[1.,2.];memo={}
        first=deepcopy({'a':shared},memo)
        second=deepcopy({'b':shared},memo)
        self.assertIs(first['a'],second['b'])
        self.assertIsNot(first['a'],shared)
        self.assertIn(shared,memo[id(memo)])

    def test_custom_key_and_value_copy_order_matches_standard(self):
        events=[]
        class Key:
            def __deepcopy__(self,memo):events.append('key');return Key()
        class Value:
            def __deepcopy__(self,memo):events.append('value');return Value()
        root={Key():Value()}
        copy.deepcopy(root);expected=list(events);events.clear()
        deepcopy(root)
        self.assertEqual(events,expected)

    def test_random_native_graphs_match_standard_without_type_coercion(self):
        rng=random.Random(20261007)
        def node(depth):
            if depth==0:return rng.choice([None,True,12.693,10**40,'source',b'bytes'])
            values=[node(depth-1) for _ in range(rng.randrange(1,5))]
            return values if rng.random()<.5 else {str(i):value for i,value in enumerate(values)}
        for _ in range(100):
            value=node(4)
            self.assertEqual(deepcopy(value),copy.deepcopy(value))

    def test_immutable_tuple_identity_is_delegated_unchanged(self):
        value=('source',12.727,True)
        self.assertIs(deepcopy({'tuple':value})['tuple'],copy.deepcopy(value))


if __name__=='__main__':unittest.main()
