# Copyright (c) 2019 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

from collections import defaultdict
import copy
import uuid
from typing import Dict, Optional, TYPE_CHECKING, Any, List, cast

from PyQt5.Qt import QTimer, QObject, pyqtSignal, pyqtSlot

from UM.Decorators import deprecated
from UM.Logger import Logger
from UM.Settings.ContainerRegistry import ContainerRegistry
from UM.Util import parseBool
import cura.CuraApplication  # Imported like this to prevent circular imports.
from cura.Machines.ContainerTree import ContainerTree
from cura.Settings.CuraContainerRegistry import CuraContainerRegistry

from .MaterialNode import MaterialNode
from .MaterialGroup import MaterialGroup

if TYPE_CHECKING:
    from UM.Settings.DefinitionContainer import DefinitionContainer
    from UM.Settings.InstanceContainer import InstanceContainer
    from cura.Settings.GlobalStack import GlobalStack
    from cura.Settings.ExtruderStack import ExtruderStack


#
# MaterialManager maintains a number of maps and trees for material lookup.
# The models GUI and QML use are now only dependent on the MaterialManager. That means as long as the data in
# MaterialManager gets updated correctly, the GUI models should be updated correctly too, and the same goes for GUI.
#
# For now, updating the lookup maps and trees here is very simple: we discard the old data completely and recreate them
# again. This means the update is exactly the same as initialization. There are performance concerns about this approach
# but so far the creation of the tables and maps is very fast and there is no noticeable slowness, we keep it like this
# because it's simple.
#
class MaterialManager(QObject):
    __instance = None

    @classmethod
    @deprecated("Use the ContainerTree structure instead.", since = "4.3")
    def getInstance(cls) -> "MaterialManager":
        if cls.__instance is None:
            cls.__instance = MaterialManager()
        return cls.__instance

    materialsUpdated = pyqtSignal()  # Emitted whenever the material lookup tables are updated.
    favoritesUpdated = pyqtSignal()  # Emitted whenever the favorites are changed

    def __init__(self, parent = None):
        super().__init__(parent)
        # Material_type -> generic material metadata
        self._fallback_materials_map = dict()  # type: Dict[str, Dict[str, Any]]

        # Root_material_id -> MaterialGroup
        self._material_group_map = dict()  # type: Dict[str, MaterialGroup]

        # Material id including diameter (generic_pla_175) -> material root id (generic_pla)
        self._diameter_material_map = dict()  # type: Dict[str, str]

        # This is used in Legacy UM3 send material function and the material management page.
        # GUID -> a list of material_groups
        self._guid_material_groups_map = defaultdict(list)  # type: Dict[str, List[MaterialGroup]]

        self._favorites = set(cura.CuraApplication.CuraApplication.getInstance().getPreferences().getValue("cura/favorite_materials").split(";"))
        self.materialsUpdated.emit()

        self._update_timer = QTimer(self)
        self._update_timer.setInterval(300)

        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self.materialsUpdated)

        container_registry = ContainerRegistry.getInstance()
        container_registry.containerMetaDataChanged.connect(self._onContainerMetadataChanged)
        container_registry.containerAdded.connect(self._onContainerMetadataChanged)
        container_registry.containerRemoved.connect(self._onContainerMetadataChanged)

    def _onContainerMetadataChanged(self, container):
        self._onContainerChanged(container)

    def _onContainerChanged(self, container):
        container_type = container.getMetaDataEntry("type")
        if container_type != "material":
            return

        # update the maps

        self._update_timer.start()

    def getMaterialGroup(self, root_material_id: str) -> Optional[MaterialGroup]:
        return self._material_group_map.get(root_material_id)

    def getRootMaterialIDForDiameter(self, root_material_id: str, approximate_diameter: str) -> str:
        original_material = CuraContainerRegistry.getInstance().findInstanceContainersMetadata(id=root_material_id)[0]
        if original_material["approximate_diameter"] == approximate_diameter:
            return root_material_id

        matching_materials = CuraContainerRegistry.getInstance().findInstanceContainersMetadata(type = "material", brand = original_material["brand"], definition = original_material["definition"], material = original_material["material"], color_name = original_material["color_name"])
        for material in matching_materials:
            if material["approximate_diameter"] == approximate_diameter:
                return material["id"]
        return root_material_id

    def getRootMaterialIDWithoutDiameter(self, root_material_id: str) -> str:
        return self._diameter_material_map.get(root_material_id, "")

    def getMaterialGroupListByGUID(self, guid: str) -> Optional[List[MaterialGroup]]:
        return self._guid_material_groups_map.get(guid)

    # Returns a dict of all material groups organized by root_material_id.
    def getAllMaterialGroups(self) -> Dict[str, "MaterialGroup"]:
        return self._material_group_map

    ##  Gives a dictionary of all root material IDs and their associated
    #   MaterialNodes from the ContainerTree that are available for the given
    #   printer and variant.
    def getAvailableMaterials(self, definition_id: str, nozzle_name: Optional[str]) -> Dict[str, MaterialNode]:
        return ContainerTree.getInstance().machines[definition_id].variants[nozzle_name].materials

    #
    # A convenience function to get available materials for the given machine with the extruder position.
    #
    def getAvailableMaterialsForMachineExtruder(self, machine: "GlobalStack",
                                                extruder_stack: "ExtruderStack") -> Dict[str, MaterialNode]:
        nozzle_name = None
        if extruder_stack.variant.getId() != "empty_variant":
            nozzle_name = extruder_stack.variant.getName()

        # Fetch the available materials (ContainerNode) for the current active machine and extruder setup.
        materials = self.getAvailableMaterials(machine.definition.getId(), nozzle_name)
        compatible_material_diameter = extruder_stack.getApproximateMaterialDiameter()
        result = {key: material for key, material in materials.items() if material.container and float(material.container.getMetaDataEntry("approximate_diameter")) == compatible_material_diameter}
        return result

    #
    # Gets MaterialNode for the given extruder and machine with the given material name.
    # Returns None if:
    #  1. the given machine doesn't have materials;
    #  2. cannot find any material InstanceContainers with the given settings.
    #
    def getMaterialNode(self, machine_definition_id: str, nozzle_name: Optional[str],
                        buildplate_name: Optional[str], diameter: float, root_material_id: str) -> Optional["MaterialNode"]:
        container_tree = ContainerTree.getInstance()
        machine_node = container_tree.machines.get(machine_definition_id)
        if machine_node is None:
            Logger.log("w", "Could not find machine with definition %s in the container tree", machine_definition_id)
            return None

        variant_node = machine_node.variants.get(nozzle_name)
        if variant_node is None:
            Logger.log("w", "Could not find variant %s for machine with definition %s in the container tree", nozzle_name, machine_definition_id )
            return None

        material_node = variant_node.materials.get(root_material_id)

        if material_node is None:
            Logger.log("w", "Could not find material %s for machine with definition %s and variant %s in the container tree", root_material_id, machine_definition_id, nozzle_name)
            return None

        return material_node

    #
    # Gets MaterialNode for the given extruder and machine with the given material type.
    # Returns None if:
    #  1. the given machine doesn't have materials;
    #  2. cannot find any material InstanceContainers with the given settings.
    #
    def getMaterialNodeByType(self, global_stack: "GlobalStack", position: str, nozzle_name: str,
                              buildplate_name: Optional[str], material_guid: str) -> Optional["MaterialNode"]:
        machine_definition = global_stack.definition
        extruder = global_stack.extruderList[int(position)]
        variant_name = extruder.variant.getName()
        approximate_diameter = extruder.getApproximateMaterialDiameter()

        return self.getMaterialNode(machine_definition.getId(), variant_name, buildplate_name, approximate_diameter, material_guid)

    #   There are 2 ways to get fallback materials;
    #   - A fallback by type (@sa getFallbackMaterialIdByMaterialType), which adds the generic version of this material
    #   - A fallback by GUID; If a material has been duplicated, it should also check if the original materials do have
    #       a GUID. This should only be done if the material itself does not have a quality just yet.
    def getFallBackMaterialIdsByMaterial(self, material: "InstanceContainer") -> List[str]:
        results = []  # type: List[str]

        material_groups = self.getMaterialGroupListByGUID(material.getMetaDataEntry("GUID"))
        for material_group in material_groups:  # type: ignore
            if material_group.name != material.getId():
                # If the material in the group is read only, put it at the front of the list (since that is the most
                # likely one to get a result)
                if material_group.is_read_only:
                    results.insert(0, material_group.name)
                else:
                    results.append(material_group.name)

        fallback = self.getFallbackMaterialIdByMaterialType(material.getMetaDataEntry("material"))
        if fallback is not None:
            results.append(fallback)
        return results

    #
    # Built-in quality profiles may be based on generic material IDs such as "generic_pla".
    # For materials such as ultimaker_pla_orange, no quality profiles may be found, so we should fall back to use
    # the generic material IDs to search for qualities.
    #
    # An example would be, suppose we have machine with preferred material set to "filo3d_pla" (1.75mm), but its
    # extruders only use 2.85mm materials, then we won't be able to find the preferred material for this machine.
    # A fallback would be to fetch a generic material of the same type "PLA" as "filo3d_pla", and in this case it will
    # be "generic_pla". This function is intended to get a generic fallback material for the given material type.
    #
    # This function returns the generic root material ID for the given material type, where material types are "PLA",
    # "ABS", etc.
    #
    def getFallbackMaterialIdByMaterialType(self, material_type: str) -> Optional[str]:
        # For safety
        if material_type not in self._fallback_materials_map:
            Logger.log("w", "The material type [%s] does not have a fallback material" % material_type)
            return None
        fallback_material = self._fallback_materials_map[material_type]
        if fallback_material:
            return self.getRootMaterialIDWithoutDiameter(fallback_material["id"])
        else:
            return None

    ##  Get default material for given global stack, extruder position and extruder nozzle name
    #   you can provide the extruder_definition and then the position is ignored (useful when building up global stack in CuraStackBuilder)
    def getDefaultMaterial(self, global_stack: "GlobalStack", position: str, nozzle_name: Optional[str],
                           extruder_definition: Optional["DefinitionContainer"] = None) -> "MaterialNode":
        definition_id = global_stack.definition.getId()
        machine_node = ContainerTree.getInstance().machines[definition_id]
        if nozzle_name in machine_node.variants:
            nozzle_node = machine_node.variants[nozzle_name]
        else:
            Logger.log("w", "Could not find variant {nozzle_name} for machine with definition {definition_id} in the container tree".format(nozzle_name = nozzle_name, definition_id = definition_id))
            nozzle_node = next(iter(machine_node.variants))

        if not parseBool(global_stack.getMetaDataEntry("has_materials", False)):
            return next(iter(nozzle_node.materials))

        if extruder_definition is not None:
            material_diameter = extruder_definition.getProperty("material_diameter", "value")
        else:
            material_diameter = global_stack.extruders[position].getCompatibleMaterialDiameter()
        approximate_material_diameter = round(material_diameter)

        return nozzle_node.preferredMaterial(approximate_material_diameter)

    def removeMaterialByRootId(self, root_material_id: str):
        container_registry = CuraContainerRegistry.getInstance()
        results = container_registry.findContainers(id = root_material_id)
        if not results:
            container_registry.addWrongContainerId(root_material_id)

        for result in results:
            container_registry.removeContainer(result.getMetaDataEntry("id", ""))

    @pyqtSlot("QVariant", result = bool)
    def canMaterialBeRemoved(self, material_node: "MaterialNode"):
        # Check if the material is active in any extruder train. In that case, the material shouldn't be removed!
        # In the future we might enable this again, but right now, it's causing a ton of issues if we do (since it
        # corrupts the configuration)
        root_material_id = material_node.base_file
        ids_to_remove = {metadata.get("id", "") for metadata in CuraContainerRegistry.getInstance().findInstanceContainersMetadata(base_file = root_material_id)}

        for extruder_stack in CuraContainerRegistry.getInstance().findContainerStacks(type = "extruder_train"):
            if extruder_stack.material.getId() in ids_to_remove:
                return False
        return True

    ##  Change the user-visible name of a material.
    #   \param material_node The ContainerTree node of the material to rename.
    #   \param name The new name for the material.
    @pyqtSlot("QVariant", str)
    def setMaterialName(self, material_node: "MaterialNode", name: str) -> None:
        return cura.CuraApplication.CuraApplication.getMaterialManagementModel().setMaterialName(material_node, name)

    ##  Deletes a material from Cura.
    #
    #   This function does not do any safety checking any more. Please call this
    #   function only if:
    #   - The material is not read-only.
    #   - The material is not used in any stacks.
    #   If the material was not lazy-loaded yet, this will fully load the
    #   container. When removing this material node, all other materials with
    #   the same base fill will also be removed.
    #   \param material_node The material to remove.
    @pyqtSlot("QVariant")
    def removeMaterial(self, material_node: "MaterialNode") -> None:
        return cura.CuraApplication.CuraApplication.getMaterialManagementModel().setMaterialName(material_node)

    def duplicateMaterialByRootId(self, root_material_id: str, new_base_id: Optional[str] = None, new_metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
        result = cura.CuraApplication.CuraApplication.getInstance().getMaterialManagementModel().duplicateMaterialByBaseFile(root_material_id, new_base_id, new_metadata)
        if result is None:
            return "ERROR"
        return result

    ##  Creates a duplicate of a material with the same GUID and base_file
    #   metadata.
    #   \param material_node The node representing the material to duplicate.
    #   \param new_base_id A new material ID for the base material. The IDs of
    #   the submaterials will be based off this one. If not provided, a material
    #   ID will be generated automatically.
    #   \param new_metadata Metadata for the new material. If not provided, this
    #   will be duplicated from the original material.
    #   \return The root material ID of the duplicate material.
    @pyqtSlot("QVariant", result = str)
    def duplicateMaterial(self, material_node: MaterialNode, new_base_id: Optional[str] = None, new_metadata: Optional[Dict[str, Any]] = None) -> str:
        result = cura.CuraApplication.CuraApplication.getInstance().getMaterialManagementModel().duplicateMaterial(material_node, new_base_id, new_metadata)
        if result is None:
            return "ERROR"
        return result

    ##  Create a new material by cloning the preferred material for the current
    #   material diameter and generate a new GUID.
    #
    #   The material type is explicitly left to be the one from the preferred
    #   material, since this allows the user to still have SOME profiles to work
    #   with.
    #   \return The ID of the newly created material.
    @pyqtSlot(result = str)
    def createMaterial(self) -> str:
        return cura.CuraApplication.CuraApplication.getMaterialManagementModel().createMaterial()

    @pyqtSlot(str)
    def addFavorite(self, root_material_id: str) -> None:
        self._favorites.add(root_material_id)
        self.materialsUpdated.emit()

        # Ensure all settings are saved.
        cura.CuraApplication.CuraApplication.getInstance().getPreferences().setValue("cura/favorite_materials", ";".join(list(self._favorites)))
        cura.CuraApplication.CuraApplication.getInstance().saveSettings()

    @pyqtSlot(str)
    def removeFavorite(self, root_material_id: str) -> None:
        try:
            self._favorites.remove(root_material_id)
        except KeyError:
            Logger.log("w", "Could not delete material %s from favorites as it was already deleted", root_material_id)
            return
        self.materialsUpdated.emit()

        # Ensure all settings are saved.
        cura.CuraApplication.CuraApplication.getInstance().getPreferences().setValue("cura/favorite_materials", ";".join(list(self._favorites)))
        cura.CuraApplication.CuraApplication.getInstance().saveSettings()

    @pyqtSlot()
    def getFavorites(self):
        return self._favorites
