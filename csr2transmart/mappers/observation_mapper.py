from typing import List, Dict, Optional, Type, Any
from pydantic import BaseModel
from transmart_loader.transmart import TrialVisit, Patient, Concept, Modifier, Observation, ObservationMetadata, \
    Value, CategoricalValue, ValueType, NumericalValue, DateValue, TextValue

from csr.csr import CentralSubjectRegistry, StudyRegistry, Individual, SubjectEntity

from csr.exceptions import MappingException


class ObservationMapper:
    """
    Map observations for subject registry and study registry
    """
    def __init__(self,
                 subject_registry: CentralSubjectRegistry,
                 study_registry: StudyRegistry,
                 default_trial_visit: TrialVisit,
                 individual_id_to_patient: Dict[str, Patient],
                 concept_code_to_concept: Dict[str, Concept],
                 modifier_key_to_modifier: Dict[str, Modifier]):
        self.subject_registry = subject_registry
        self.study_registry = study_registry
        self.default_trial_visit = default_trial_visit
        self.individual_id_to_patient = individual_id_to_patient
        self.concept_code_to_concept = concept_code_to_concept
        self.modifier_key_to_modifier = modifier_key_to_modifier
        self.observations: List[Observation] = []

    @staticmethod
    def row_value_to_value(row_value, value_type: ValueType) -> Optional[Value]:
        """
        Map entity value to transmart-loader Value by ValueType
        :param row_value: entity value
        :param value_type: transmart-loader ValueType
        :return: transmart-loader Value
        """
        if row_value is None:
            return None
        if value_type is ValueType.Categorical:
            return CategoricalValue(row_value)
        elif value_type is ValueType.Numeric:
            return NumericalValue(row_value)
        elif value_type is ValueType.DateValue:
            return DateValue(row_value)
        else:
            return TextValue(row_value)

    @staticmethod
    def skip_reference(entity_type: Type[BaseModel], ref_type: str) -> bool:
        return ref_type == entity_type.schema()['title']

    @staticmethod
    def get_field_properties_by_keyword(entity_type: Type[BaseModel], key: str) -> Dict[str, Any]:
        """
        Get name of the entities by the schema metadata
        :param entity_type: type of the CSR entity
        :param key: schema keyword key
        :param value: schema keyword value
        :return:
        """
        return {name: prop[key] for (name, prop) in entity_type.schema()['properties'].items() if key in prop}

    @staticmethod
    def get_field_names_by_key_and_value(entity_type: Type[BaseModel], key: str, value) -> List[str]:
        """
        Get name of the field of CSR entity type by the schema metadata
        :param entity_type: type of the CSR entity
        :param key: schema keyword key
        :param value: schema keyword value
        :return:
        """
        return list([name for (name, prop) in entity_type.schema()['properties'].items()
                     if key in prop and prop[key] is value])

    def get_id_field_name(self, entity_type: Type[BaseModel]) -> str:
        """
        Get identifying field name of a CSR entity type by 'identity' schema keyword
        :param entity_type: type of the CSR entity
        :return:
        """
        return self.get_field_names_by_key_and_value(entity_type, 'identity', True)[0]

    def get_ref_entity_name_to_ref_field_value(self,
                                               entity: BaseModel,
                                               entity_type: Type[BaseModel]) -> Dict[str, str]:
        """
        Get a dictionary with name of reference entities to value of the referencing field,
        being id of the referencing entity
        :param entity:
        :param ref_entity_type_to_ref_field_name_dict:
        :return:
        """
        entity_ref_to_ref_id = dict()
        id_attribute = self.get_id_field_name(entity_type)
        entity_type_name = entity_type.schema()['title']
        entity_id = entity.__getattribute__(id_attribute)
        entity_ref_to_ref_id[entity_type_name] = entity_id

        if entity_type_name == 'Individual':
            return entity_ref_to_ref_id

        # Follow reference fields to obtain identifiers of linked entities
        ref_fields = self.get_field_properties_by_keyword(entity_type, 'references')
        for field_name, ref_entity_name in ref_fields.items():
            if not self.skip_reference(type(entity), ref_entity_name):
                # Lookup referenced entity
                referenced_entity_type = list([entity for entity in SubjectEntity.__args__
                                               if entity.schema()['title'] == ref_entity_name])[0]
                referenced_id = entity.__getattribute__(field_name)
                if not referenced_id:
                    continue
                referenced_id_attribute = self.get_id_field_name(referenced_entity_type)
                referenced_entities = [e for e in self.subject_registry.entity_data[ref_entity_name]
                                       if e.__getattribute__(referenced_id_attribute) == referenced_id]
                if not referenced_entities:
                    raise MappingException(
                        f'{entity_type_name} with id {entity_id} has reference to non-existing'
                        f' {ref_entity_name} with id {referenced_id}.')
                # Recursively add identifiers from referenced entity
                referenced_ids = self.get_ref_entity_name_to_ref_field_value(referenced_entities[0],
                                                                             referenced_entity_type)
                entity_ref_to_ref_id.update(referenced_ids)

        return entity_ref_to_ref_id

    def map_observation_metadata(self, entity_type_to_id: Dict[str, str]) -> Optional[ObservationMetadata]:
        """
        Get observation modifier
        :param entity_type_to_id: observation modifier key to value of the modifier observation
        :return: transmart-loader metadata if any
        """
        mod_metadata: Dict[Modifier, Value] = dict()
        for modifier_key, value in entity_type_to_id.items():
            modifier = self.modifier_key_to_modifier.get(modifier_key)
            if modifier is None:
                return None
            mod_metadata[modifier] = CategoricalValue(value)
        return ObservationMetadata(mod_metadata)

    def get_observation_for_value(self, row_value, concept: Concept, metadata: ObservationMetadata,
                                  patient: Patient) -> Observation:
        value = self.row_value_to_value(row_value, concept.value_type)
        return Observation(patient, concept, None, self.default_trial_visit, None, None, value, metadata)

    def map_observation(self,
                        entity: BaseModel,
                        entity_id: str,
                        entity_type_to_id: Dict[str, str]):
        """
        Map entity to trasmart-loader Observation
        :param entity: CSR entity
        :param entity_id: id of the entity
        :param entity_type_to_id:
        :return:
        """
        entity_fields = entity.fields.keys()
        print("Entity fields: " + str(entity_fields))
        entity_name = entity.schema()['title']
        individual_id = entity_type_to_id.pop('Individual', None)
        patient = self.individual_id_to_patient.get(individual_id, None)
        if not patient:
            raise MappingException('No patient with identifier: {}. '
                                   'Failed to create observation for {} with id: {}. Entity {}, Ind {}'
                                   .format(individual_id, type(entity).__name__, entity_id,
                                           entity, self.individual_id_to_patient.keys()))
        for entity_field in entity_fields:
            concept_code = '{}.{}'.format(entity_name, entity_field)
            concept = self.concept_code_to_concept.get(concept_code)
            if concept is not None:
                if isinstance(entity, Individual) or not entity_type_to_id:
                    metadata = None
                else:
                    metadata = self.map_observation_metadata(entity_type_to_id)
                entity_value = getattr(entity, entity_field)
                if entity_value is not None:
                    if isinstance(entity_value, List):
                        for v in entity_value:
                            self.observations.append(
                                self.get_observation_for_value(v, concept, metadata, patient))
                    else:
                        self.observations.append(
                            self.get_observation_for_value(entity_value, concept, metadata, patient))

    def map_non_individual_linked_entity_observations(self, entity_type: Type[BaseModel]):
        """
        Map observations for entities that do not have a direct link to individuals,
        but have a reference linking to individuals.
        :param entity_type: Central subject registry entity type
        :return:
        """
        entities = self.subject_registry.entity_data[entity_type.schema()['title']]
        if not entities:
            return

        entity_id_field_name = self.get_id_field_name(entity_type)
        for entity in entities:
            entity_id = entity.__getattribute__(entity_id_field_name)
            entity_type_to_id = self.get_ref_entity_name_to_ref_field_value(entity, entity_type)
            self.map_observation(entity, entity_id, entity_type_to_id)

    def map_study_registry_observations(self):
        """
        Map observations for Subject registry entities
        :param study_registry: Study registry
        :return:
        """
        for ind_study in self.study_registry.entity_data['IndividualStudy']:
            study = next((s for s in self.study_registry.entity_data['Study']
                          if s.study_id == ind_study.study_id), None)
            if study is None:
                raise MappingException('No study with identifier: {}. '
                                       'Failed to create observation for individual study with id: {}.'
                                       .format(ind_study.study_id, ind_study.individual_id))
            entity_type_to_id = {'Individual': ind_study.individual_id}
            print("Entity type 2 id: " + str(entity_type_to_id.keys()) + ": " + str(entity_type_to_id.values()))
            self.map_observation(study, study.study_id, entity_type_to_id)

    def map_subject_registry_observations(self, entity_type: Type[BaseModel]):
        """
        Map observations for Central subject registry entities
        :param subject_registry: Central subject registry
        :param entity_type:
        :return:
        """
        self.map_non_individual_linked_entity_observations(entity_type)

    def map_observations(self):
        """
        Map observations for each csr entity
        :param subject_registry: Central subject registry
        :param study_registry: Study registry
        :return:
        """
        entities = list(SubjectEntity.__args__)
        for entity_type in entities:
            self.map_subject_registry_observations(entity_type)
        self.map_study_registry_observations()
